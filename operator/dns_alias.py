import dataclasses
import os
import re

# Every alias this operator creates gets this description prefix, so a reconcile can tell "a
# record we created" apart from "a record someone made by hand in the OPNsense UI" and never
# overwrite the latter.
OWNER_TAG_PREFIX = "managed-by=dns-operator;owner="

# A single DNS label: alphanumeric, may contain internal hyphens/underscores, must not start or
# end with one, 1-63 chars. Deliberately excludes "*" - without this, `hostname.partition(".")`
# alone would accept a wildcard hostname like "*.apps.example.internal" as long as its domain is
# on the allowlist, letting one tenant claim every unclaimed name in a shared domain.
_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$")


class DNSPolicyError(Exception):
    """Raised when a requested alias violates the operator's domain/target allowlists, or would
    conflict with a DNS record this operator doesn't own.

    Carries two messages: `str(exc)` (`args[0]`) is safe to write back onto the *requesting*
    object's own status/annotation - any tenant who can read their own Ingress/DNSAlias can read
    it, so it never names another namespace's owning resource or dumps the operator's full
    domain/target allowlists. `detail` carries the complete message, including whatever the safe
    message omits, and is only ever meant for the operator's own logs (pod logs are cluster-admin
    visible, not tenant visible).
    """

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = message if detail is None else detail


def _split_csv(value):
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclasses.dataclass
class Config:
    allowed_domains: list
    allowed_targets: list
    default_target: str
    adopt_unmanaged: bool

    @classmethod
    def from_env(cls):
        allowed_domains = _split_csv(os.environ["DNS_ALLOWED_DOMAINS"])
        allowed_targets = _split_csv(os.environ["DNS_ALLOWED_TARGETS"])
        default_target = os.environ.get("DNS_DEFAULT_TARGET", "").strip() or (
            allowed_targets[0] if allowed_targets else ""
        )
        if default_target not in allowed_targets:
            raise ValueError(
                f"DNS_DEFAULT_TARGET {default_target!r} must be one of DNS_ALLOWED_TARGETS {allowed_targets}"
            )
        return cls(
            allowed_domains=allowed_domains,
            allowed_targets=allowed_targets,
            default_target=default_target,
            adopt_unmanaged=os.environ.get("DNS_ADOPT_UNMANAGED", "false").strip().lower() == "true",
        )


def _validate_domain(domain):
    labels = domain.split(".")
    if not all(_LABEL_RE.match(label) for label in labels):
        raise DNSPolicyError(f"{domain!r} is not a valid domain name")


def split_fqdn(fqdn):
    hostname, _, domain = fqdn.partition(".")
    if not domain:
        raise DNSPolicyError(f"{fqdn!r} must be a fully-qualified hostname (hostname.domain)")
    if not _LABEL_RE.match(hostname):
        raise DNSPolicyError(f"{hostname!r} is not a valid DNS hostname label")
    _validate_domain(domain)
    return hostname, domain


def validate_request(config: Config, fqdn: str, target: str):
    hostname, domain = split_fqdn(fqdn)
    if domain not in config.allowed_domains:
        raise DNSPolicyError(
            f"domain {domain!r} is not in the operator's allowed domain list",
            detail=f"domain {domain!r} is not in the allowed domain list {config.allowed_domains}",
        )
    if target not in config.allowed_targets:
        raise DNSPolicyError(
            f"target {target!r} is not in the operator's allowed target list",
            detail=f"target {target!r} is not in the allowed target list {config.allowed_targets}",
        )
    return hostname, domain


def owner_tag(namespace, kind, name):
    return f"{OWNER_TAG_PREFIX}{namespace}/{kind}/{name}"


def owned_by(description, namespace, kind, name):
    return description == owner_tag(namespace, kind, name)


def is_operator_managed(description):
    return isinstance(description, str) and description.startswith(OWNER_TAG_PREFIX)


def reconcile_alias(client, config, namespace, kind, name, fqdn, target=None):
    """Ensure `fqdn` exists as an alias to `target` (or the configured default), owned by
    (namespace, kind, name). Raises DNSPolicyError without changing anything in OPNsense if the
    request is out of policy or would conflict with a record this operator doesn't own.

    If `config.adopt_unmanaged` is set, a pre-existing alias that isn't operator-managed is taken
    over (re-tagged as owned by this resource) rather than rejected - but only when it already
    points at the requested target. An unmanaged alias pointing somewhere else is still a conflict:
    adoption claims a record that already agrees with what's being requested, it doesn't repoint
    one that doesn't.
    """
    target = target or config.default_target
    hostname, domain = validate_request(config, fqdn, target)
    tag = owner_tag(namespace, kind, name)

    target_hostname, target_domain = split_fqdn(target)
    parent = client.find_override(target_hostname, target_domain)
    if parent is None:
        raise DNSPolicyError(f"alias target {target!r} is not an existing OPNsense host override")

    existing = client.find_alias(hostname, domain)
    if existing is not None and not owned_by(existing.get("description", ""), namespace, kind, name):
        if is_operator_managed(existing.get("description", "")):
            raise DNSPolicyError(
                f"{fqdn} is already managed by another resource",
                detail=f"{fqdn} is already managed by another resource ({existing['description']})",
            )
        if not (config.adopt_unmanaged and existing["_parent_uuid"] == parent["uuid"]):
            raise DNSPolicyError(
                f"{fqdn} already exists as a DNS alias not created by this operator - refusing to touch it"
            )
        client.set_alias(existing["uuid"], parent["uuid"], hostname, domain, tag)
        client.reconfigure()
        return "adopted", f"{fqdn} -> {target} (adopted pre-existing alias)"

    if existing is None:
        client.add_alias(parent["uuid"], hostname, domain, tag)
        client.reconfigure()
        return "created", f"{fqdn} -> {target} (created)"

    if existing["_parent_uuid"] != parent["uuid"]:
        client.set_alias(existing["uuid"], parent["uuid"], hostname, domain, tag)
        client.reconfigure()
        return "updated", f"{fqdn} -> {target} (updated)"

    return "unchanged", f"{fqdn} -> {target} (unchanged)"


def delete_alias(client, namespace, kind, name, fqdn):
    """Delete the alias for `fqdn` only if it's still owned by (namespace, kind, name). A record
    that's been reassigned or hand-edited since is left alone rather than deleted."""
    hostname, domain = split_fqdn(fqdn)
    existing = client.find_alias(hostname, domain)
    if existing is None:
        return "absent"
    if not owned_by(existing.get("description", ""), namespace, kind, name):
        return "skipped-not-owned"
    client.delete_alias(existing["uuid"])
    client.reconfigure()
    return "deleted"
