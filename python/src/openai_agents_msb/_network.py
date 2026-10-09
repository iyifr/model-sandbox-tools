"""Turning MST's network shorthand into a microsandbox policy."""

from __future__ import annotations

import ipaddress
import re

from microsandbox import (
    DestGroup,
    Destination,
    Network,
    NetworkDestination,
    NetworkDestinationKind,
    NetworkPolicy,
    NetworkProfile,
    Rule,
    Secret,
    SecretEntry,
    TlsConfig,
)

from .core import Allow, SandboxConfig

__all__ = ["resolve_network", "to_secret_entries", "validate_network"]

PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")

# Labels of 1-63 chars, letters/digits/hyphens, not starting or ending with a hyphen.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")


def _normalize(network: object) -> object:
    """Collapse the boolean shorthand onto the string one."""
    if network is True:
        return "public"
    if network is False:
        return "none"
    return network


def _destination(entry: str) -> NetworkDestination:
    if "/" in entry:
        return Destination.cidr(entry)
    try:
        ipaddress.ip_address(entry)
    except ValueError:
        pass
    else:
        return Destination.ip(entry)
    if entry.startswith("*."):
        return Destination.domain_suffix(entry[2:])
    return Destination.domain(entry)


def _assert_valid_host(entry: str) -> None:
    """Reject malformed allowlist entries, which microsandbox only rejects at create time."""
    if not entry:
        raise ValueError("[mst] Empty network allowlist entry")
    if "/" in entry:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as err:
            raise ValueError(f"[mst] Invalid CIDR in network allowlist: {entry!r} ({err})") from err
        return
    host = entry[2:] if entry.startswith("*.") else entry
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return
    if not _HOSTNAME.match(host):
        raise ValueError(f"[mst] Invalid host in network allowlist: {entry!r}")


def _allowlist(config: SandboxConfig) -> list[str]:
    """Explicit hosts, plus the hosts that `secrets` and `packages` need."""
    network = _normalize(config.network)
    requested = list(network.hosts) if isinstance(network, Allow) else []
    hosts = [
        *requested,
        *(s.host for s in config.secrets),
        *(PYPI_HOSTS if config.packages else ()),
    ]
    seen: dict[str, None] = {}
    for host in hosts:
        seen.setdefault(host, None)
    return list(seen)


def _needs_tls(config: SandboxConfig, policy: NetworkPolicy) -> bool:
    """HTTPS domain rules match on the TLS hostname, and secret substitution rewrites
    requests, so both need microsandbox to terminate TLS on 443."""
    if config.secrets:
        return True
    hostname_kinds = (NetworkDestinationKind.DOMAIN, NetworkDestinationKind.DOMAIN_SUFFIX)
    return any(
        isinstance(rule.destination, NetworkDestination) and rule.destination.kind in hostname_kinds
        for rule in policy.rules
    )


def resolve_network(config: SandboxConfig) -> Network:
    """The microsandbox `Network` for a config. Denies everything by default."""
    network = _normalize(config.network)

    if isinstance(network, Network):
        return network
    if network == "none":
        return Network.none()
    if network == "public":
        policy = NetworkPolicy.from_profiles([NetworkProfile.PUBLIC])
    elif network is None or isinstance(network, Allow):
        hosts = _allowlist(config)
        if not hosts:
            return Network.none()
        for host in hosts:
            _assert_valid_host(host)
        policy = NetworkPolicy(rules=(*Rule.allow_dns(), *(Rule.allow(destination=_destination(h)) for h in hosts)))
    else:
        raise ValueError(
            f"[mst] Invalid network config {network!r}: expected 'none', 'public', a bool, "
            "Allow([...]) or a microsandbox Network"
        )

    tls = TlsConfig(intercepted_ports=(443,)) if _needs_tls(config, policy) else None
    return Network(policy=policy, tls=tls)


def _denies_everything(network: Network) -> bool:
    return isinstance(network.policy, NetworkPolicy) and not network.policy.rules


def validate_network(config: SandboxConfig) -> None:
    """Reject bad network settings before any VM is created."""
    network = resolve_network(config)
    if _denies_everything(network) and (config.packages or config.secrets):
        raise ValueError(
            "[mst] network='none' is incompatible with `packages` and `secrets` -- "
            "they require network access. Leave `network` unset to allow only the hosts they need."
        )
    # microsandbox type-checks the policy when it is serialized, which otherwise
    # would not happen until Sandbox.create().
    try:
        network._to_dict()
    except Exception as err:
        raise ValueError(f"[mst] Invalid network config {config.network!r}: {err}") from err


def to_secret_entries(config: SandboxConfig) -> list[SecretEntry]:
    """MST secrets as microsandbox entries, each scoped to its own host."""
    return [
        Secret.env(secret.env, value=secret.value, allow=[secret.host])
        for secret in config.secrets
    ]
