"""Network shorthand -> microsandbox policy."""

from __future__ import annotations

import pytest
from microsandbox import (
    Action,
    DestGroup,
    Destination,
    Network,
    NetworkDestination,
    NetworkDestinationKind,
    NetworkPolicy,
    Rule,
)

from openai_agents_msb import Allow, SandboxConfig, SandboxSecret
from openai_agents_msb._network import resolve_network, to_secret_entries, validate_network


def config(**kwargs: object) -> SandboxConfig:
    return SandboxConfig(image="python:3.12-slim", interpreter="python3", **kwargs)  # type: ignore[arg-type]


def destinations(network: Network) -> list[tuple[NetworkDestinationKind, str | None]]:
    assert isinstance(network.policy, NetworkPolicy)
    return [
        (rule.destination.kind, rule.destination.value)
        for rule in network.policy.rules
        if isinstance(rule.destination, NetworkDestination)
    ]


def test_default_is_no_network() -> None:
    network = resolve_network(config())
    assert network.policy == NetworkPolicy.none()


@pytest.mark.parametrize("value", ["none", False])
def test_none_shorthand(value: object) -> None:
    assert resolve_network(config(network=value)).policy == NetworkPolicy.none()


@pytest.mark.parametrize("value", ["public", True])
def test_public_shorthand_allows_only_the_public_group(value: object) -> None:
    network = resolve_network(config(network=value))
    assert isinstance(network.policy, NetworkPolicy)
    assert network.policy.default_egress == Action.DENY
    assert (NetworkDestinationKind.GROUP, DestGroup.PUBLIC.value) in destinations(network)
    # No TLS interception: the public group matches on address, not hostname.
    assert network.tls is None


def test_dns_is_opened_for_hostname_allowlists() -> None:
    network = resolve_network(config(network=Allow(["api.example.com"])))
    assert (NetworkDestinationKind.GROUP, DestGroup.HOST.value) in destinations(network)


def test_allow_entries_map_to_destination_kinds() -> None:
    network = resolve_network(
        config(network=Allow(["api.example.com", "*.github.com", "10.0.0.0/8", "1.2.3.4"]))
    )
    assert set(destinations(network)) >= {
        (NetworkDestinationKind.DOMAIN, "api.example.com"),
        (NetworkDestinationKind.DOMAIN_SUFFIX, "github.com"),
        (NetworkDestinationKind.CIDR, "10.0.0.0/8"),
        (NetworkDestinationKind.IP, "1.2.3.4"),
    }


def test_hostname_rules_intercept_tls_on_443() -> None:
    network = resolve_network(config(network=Allow(["api.example.com"])))
    assert network.tls is not None
    assert network.tls.intercepted_ports == (443,)


def test_cidr_only_allowlist_needs_no_tls() -> None:
    assert resolve_network(config(network=Allow(["10.0.0.0/8"]))).tls is None


def test_packages_allow_pypi() -> None:
    network = resolve_network(config(packages=["python-docx"]))
    assert set(destinations(network)) >= {
        (NetworkDestinationKind.DOMAIN, "pypi.org"),
        (NetworkDestinationKind.DOMAIN, "files.pythonhosted.org"),
    }


def test_secret_hosts_are_allowed_and_force_tls() -> None:
    cfg = config(secrets=[SandboxSecret(env="K", value="v", host="api.example.com")])
    network = resolve_network(cfg)
    assert (NetworkDestinationKind.DOMAIN, "api.example.com") in destinations(network)
    assert network.tls is not None


def test_allowlist_is_deduplicated() -> None:
    cfg = config(
        network=Allow(["api.example.com", "api.example.com"]),
        secrets=[SandboxSecret(env="K", value="v", host="api.example.com")],
    )
    domains = [d for d in destinations(resolve_network(cfg)) if d[1] == "api.example.com"]
    assert len(domains) == 1


def test_explicit_policy_passes_through_unchanged() -> None:
    policy = NetworkPolicy(rules=(Rule.allow(destination=Destination.group(DestGroup.PRIVATE)),))
    given = Network(policy=policy)
    assert resolve_network(config(network=given)) is given


def test_none_with_packages_raises() -> None:
    with pytest.raises(ValueError, match="incompatible with `packages`"):
        validate_network(config(network="none", packages=["requests"]))


def test_none_with_secrets_raises() -> None:
    cfg = config(network="none", secrets=[SandboxSecret(env="K", value="v", host="h.example")])
    with pytest.raises(ValueError, match="incompatible with `packages`"):
        validate_network(cfg)


@pytest.mark.parametrize(
    "entry",
    ["not a host", "10.0.0.0/99", "", "exa mple.com", "-bad.example.com"],
)
def test_malformed_allowlist_entries_raise_before_any_vm(entry: str) -> None:
    with pytest.raises(ValueError, match="network allowlist"):
        validate_network(config(network=Allow([entry])))


def test_unknown_shorthand_raises() -> None:
    with pytest.raises(ValueError, match="Invalid network config"):
        validate_network(config(network="sometimes"))


def test_valid_configs_pass_validation() -> None:
    for network in [None, "none", "public", True, False, Allow(["api.example.com"])]:
        validate_network(config(network=network))


def test_secrets_are_scoped_to_their_host() -> None:
    cfg = config(
        secrets=[
            SandboxSecret(env="A", value="1", host="a.example.com"),
            SandboxSecret(env="B", value="2", host="b.example.com"),
        ]
    )
    entries = to_secret_entries(cfg)
    assert [(e.env_var, e.value, e.allow) for e in entries] == [
        ("A", "1", ("a.example.com",)),
        ("B", "2", ("b.example.com",)),
    ]
