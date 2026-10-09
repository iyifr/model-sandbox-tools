#!/usr/bin/env bash
#
# Build, verify and publish the `openai-agents-msb` Python package.
#
#   scripts/publish-python.sh --dry-run     # build + verify, upload nothing
#   scripts/publish-python.sh --test        # publish to TestPyPI
#   scripts/publish-python.sh               # publish to PyPI
#
# Auth: export UV_PUBLISH_TOKEN with an API token (starts with "pypi-"), or pass
# --trusted-publishing always in CI. Run --help for the full pipeline.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$REPO_ROOT/python"
PKG_NAME="openai-agents-msb"
IMPORT_NAME="openai_agents_msb"

PUBLISH_URL="https://upload.pypi.org/legacy/"
CHECK_URL="https://pypi.org/simple/$PKG_NAME/"
JSON_API="https://pypi.org/pypi"
INDEX_LABEL="PyPI"

DRY_RUN=0
SKIP_TESTS=0
SKIP_SMOKE=0
ALLOW_DIRTY=0
TAG=1
TRUSTED=""

if [[ -t 1 ]]; then
  B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; R=""; G=""; Y=""; N=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$G" "$N" "$B" "$1" "$N"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '%swarn%s %s\n' "$Y" "$N" "$1" >&2; }
die()  { printf '\n%serror%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

usage() {
  sed -n '3,10p' "${BASH_SOURCE[0]}" | sed 's/^#\{1\} \{0,1\}//'
  cat <<'EOF'

Options:
  --test               Publish to TestPyPI instead of PyPI.
  --dry-run            Run every check and build the artifacts, but do not upload.
  --skip-tests         Skip mypy and pytest (not recommended).
  --skip-smoke         Skip installing the built wheel into a throwaway venv.
  --allow-dirty        Permit uncommitted or untracked changes under python/.
  --no-tag             Do not create the python-v<version> git tag.
  --trusted-publishing <automatic|always|never>
                       Forwarded to `uv publish`; use "always" in CI with OIDC.
  -h, --help           Show this help.

Pipeline:
  1. check tooling and that python/ is committed
  2. read the version, cross-check __init__.py, confirm it is not already released
  3. mypy --strict and the non-VM pytest suite
  4. clean build (wheel + sdist) via `uv build`
  5. validate metadata with `twine check --strict`, assert py.typed ships
  6. install the wheel into a clean venv and import it
  7. upload, then tag
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test)
      PUBLISH_URL="https://test.pypi.org/legacy/"
      CHECK_URL="https://test.pypi.org/simple/$PKG_NAME/"
      JSON_API="https://test.pypi.org/pypi"
      INDEX_LABEL="TestPyPI"
      ;;
    --dry-run)      DRY_RUN=1 ;;
    --skip-tests)   SKIP_TESTS=1 ;;
    --skip-smoke)   SKIP_SMOKE=1 ;;
    --allow-dirty)  ALLOW_DIRTY=1 ;;
    --no-tag)       TAG=0 ;;
    --trusted-publishing)
      [[ $# -ge 2 ]] || die "--trusted-publishing needs a value"
      TRUSTED="$2"; shift
      ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --- 1. tooling and working tree -------------------------------------------
step "Checking tooling and working tree"
command -v uv >/dev/null || die "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
info "uv $(uv --version | awk '{print $2}')"
cd "$PKG_DIR"

# --porcelain also reports untracked files, which `diff HEAD` would miss.
DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- python)"
if [[ -n "$DIRTY" ]]; then
  if [[ $ALLOW_DIRTY -eq 0 ]]; then
    printf '%s\n' "$DIRTY" | sed 's/^/    /'
    die "python/ has uncommitted or untracked changes (use --allow-dirty to override)"
  fi
  warn "publishing with uncommitted changes under python/"
else
  info "python/ is committed"
fi

# --- 2. version ------------------------------------------------------------
step "Resolving version"
VERSION="$(uv version --short)"
[[ -n "$VERSION" ]] || die "could not read the version from pyproject.toml"
info "$PKG_NAME $VERSION -> $INDEX_LABEL"

# __init__.py carries its own __version__; a mismatch ships wrong metadata.
DECLARED="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "src/$IMPORT_NAME/__init__.py")"
[[ "$DECLARED" == "$VERSION" ]] ||
  die "version mismatch: pyproject.toml says $VERSION, __init__.py says ${DECLARED:-<missing>}"
info "__init__.py __version__ agrees"

TAG_NAME="python-v$VERSION"
if [[ $TAG -eq 1 ]] && git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$TAG_NAME" >/dev/null; then
  die "tag $TAG_NAME already exists; bump the version or pass --no-tag"
fi

STATUS="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  "$JSON_API/$PKG_NAME/$VERSION/json" 2>/dev/null || echo 000)"
case "$STATUS" in
  200) die "$PKG_NAME $VERSION is already on $INDEX_LABEL; bump the version" ;;
  404) info "$VERSION is not on $INDEX_LABEL yet" ;;
  *)   warn "could not reach $INDEX_LABEL (HTTP $STATUS); --check-url will catch duplicates" ;;
esac

# --- 3. tests --------------------------------------------------------------
if [[ $SKIP_TESTS -eq 1 ]]; then
  warn "skipping mypy and pytest"
else
  step "Type checking (mypy --strict)"
  uv run --group dev mypy
  step "Running tests (non-VM)"
  uv run --group dev pytest -q -m "not vm"
  info "VM tests not run here; for a real release: (cd python && uv run pytest -m vm)"
fi

# --- 4. build --------------------------------------------------------------
step "Building wheel and sdist"
rm -rf dist
uv build
ls -1 dist | sed 's/^/    /'

shopt -s nullglob
WHEELS=(dist/*.whl); SDISTS=(dist/*.tar.gz)
shopt -u nullglob
(( ${#WHEELS[@]} == 1 && ${#SDISTS[@]} == 1 )) ||
  die "expected exactly one wheel and one sdist in python/dist"
WHEEL="${WHEELS[0]}"

# --- 5. validate artifacts -------------------------------------------------
step "Validating artifacts"
uvx twine check --strict dist/*
# A typed package that loses py.typed silently stops type-checking downstream.
uv run --no-project python - "$WHEEL" "$IMPORT_NAME" <<'PY'
import sys, zipfile
wheel, pkg = sys.argv[1], sys.argv[2]
names = zipfile.ZipFile(wheel).namelist()
if f"{pkg}/py.typed" not in names:
    sys.exit(f"wheel is missing {pkg}/py.typed")
modules = sum(n.endswith(".py") for n in names)
if not modules:
    sys.exit("wheel contains no Python modules")
print(f"    py.typed present, {modules} modules")
PY

# --- 6. smoke test ---------------------------------------------------------
if [[ $SKIP_SMOKE -eq 1 ]]; then
  warn "skipping the clean-venv smoke test"
else
  step "Installing the wheel into a clean venv"
  SMOKE_DIR="$(mktemp -d)"
  trap 'rm -rf "$SMOKE_DIR"' EXIT
  uv venv --quiet "$SMOKE_DIR/venv"
  uv pip install --quiet --python "$SMOKE_DIR/venv/bin/python" "$WHEEL"
  "$SMOKE_DIR/venv/bin/python" - "$VERSION" <<'PY'
import sys
import openai_agents_msb as m
expected = sys.argv[1]
assert m.__version__ == expected, f"installed {m.__version__}, expected {expected}"
# The API the README documents must survive packaging.
for name in ("run", "run_sync", "run_streamed", "sandbox_config", "sandbox_exec",
             "sandbox_read_file", "sandbox_write_file", "sandbox_list_files"):
    assert hasattr(m, name), f"missing export: {name}"
print(f"    imported {m.__name__} {m.__version__} from a clean venv")
PY
  rm -rf "$SMOKE_DIR"
  trap - EXIT
fi

# --- 7. publish ------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
  step "Dry run complete"
  info "artifacts are in python/dist; nothing was uploaded"
  exit 0
fi

step "Publishing to $INDEX_LABEL"
PUBLISH_ARGS=(--publish-url "$PUBLISH_URL" --check-url "$CHECK_URL")
if [[ -n "$TRUSTED" ]]; then
  PUBLISH_ARGS+=(--trusted-publishing "$TRUSTED")
elif [[ -z "${UV_PUBLISH_TOKEN:-}" ]]; then
  die "set UV_PUBLISH_TOKEN to a $INDEX_LABEL API token, or pass --trusted-publishing always"
fi
uv publish "${PUBLISH_ARGS[@]}" dist/*

# --- 8. tag ----------------------------------------------------------------
if [[ $TAG -eq 1 ]]; then
  step "Tagging $TAG_NAME"
  git -C "$REPO_ROOT" tag -a "$TAG_NAME" -m "$PKG_NAME $VERSION"
  info "push it with: git push origin $TAG_NAME"
fi

step "Published $PKG_NAME $VERSION to $INDEX_LABEL"
if [[ "$INDEX_LABEL" == "TestPyPI" ]]; then
  info "verify with:"
  info "  uv pip install --index-url https://test.pypi.org/simple/ \\"
  info "    --extra-index-url https://pypi.org/simple/ $PKG_NAME==$VERSION"
else
  info "verify with: uv pip install $PKG_NAME==$VERSION"
fi
