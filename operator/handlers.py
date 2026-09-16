import os

import dns_alias
import kopf
from kubernetes import client as kube_client
from kubernetes import config as kube_config
from opnsense import OpnsenseClient

ALIAS_ANNOTATION = "dns.rarosalion.github.io/alias"
TARGET_ANNOTATION = "dns.rarosalion.github.io/target"
STATUS_ANNOTATION = "dns.rarosalion.github.io/alias-status"

# Ingress hosts are only ever picked up when explicitly opted in via ALIAS_ANNOTATION - silently
# aliasing every Ingress host would be a surprising way to create DNS records for something that
# was only ever meant to be reached from inside the cluster.
_RESYNC_INTERVAL = 600


@kopf.on.login()
def login(**_):
    # Registering a custom login handler at all replaces kopf's own built-in default
    # (kopf.login_via_client) - which matters here, not just cosmetically. That default calls
    # kubernetes.config.load_kube_config() with no context, and is what kopf's actual watch/patch
    # traffic authenticates through - a plain @kopf.on.startup() hook that loads a specific context
    # has no effect on it at all, since it configures a separate, unrelated default client that
    # kopf's own connection machinery never consults. Confirmed 2026-09-16: with multiple
    # kubeconfig files merged via KUBECONFIG, kubectl's own "current context" and an unqualified
    # load_kube_config() resolved to two different clusters - the KUBE_CONTEXT env var below (see
    # AGENTS.md's local dev workflow) is what actually pins kopf's real connection, not the
    # equivalent-looking call in startup().
    try:
        kube_config.load_incluster_config()
    except kube_config.ConfigException:
        kube_config.load_kube_config(context=os.environ.get("KUBE_CONTEXT"))

    config = kube_client.Configuration.get_default_copy()
    header = config.get_api_key_with_prefix("BearerToken") or config.get_api_key_with_prefix(
        "authorization"
    )
    parts = header.split(" ", 1) if header else []
    scheme, token = (
        (None, None) if not parts else (None, parts[0]) if len(parts) == 1 else (parts[0], parts[1])
    )

    return kopf.ConnectionInfo(
        server=config.host,
        ca_path=config.ssl_ca_cert,
        insecure=not config.verify_ssl,
        username=config.username or None,
        password=config.password or None,
        scheme=scheme,
        token=token,
        certificate_path=config.cert_file,
        private_key_path=config.key_file,
    )


@kopf.on.startup()
def startup(memo: kopf.Memo, **_):
    memo.dns_config = dns_alias.Config.from_env()
    memo.opnsense = OpnsenseClient(
        base_url=os.environ["OPNSENSE_HOST"],
        api_key=os.environ["OPNSENSE_API_KEY"],
        api_secret=os.environ["OPNSENSE_API_SECRET"],
        verify_tls=os.environ.get("OPNSENSE_VERIFY_TLS", "true").lower() != "false",
    )


def _ingress_hosts(spec):
    return sorted({rule["host"] for rule in spec.get("rules", []) if rule.get("host")})


@kopf.on.create("networking.k8s.io", "v1", "ingresses")
@kopf.on.update("networking.k8s.io", "v1", "ingresses")
# idle=30: without this, the timer's first firing can race the initial on.create handling for a
# just-created object - both see "no existing alias yet" and both call add_alias, producing a
# duplicate (confirmed 2026-09-16). idle delays a timer's firing until the object has gone quiet
# for that long, which is more than enough clearance from a create/update's own handling.
@kopf.timer("networking.k8s.io", "v1", "ingresses", interval=_RESYNC_INTERVAL, idle=30)
def reconcile_ingress(spec, meta, namespace, name, patch, logger, memo: kopf.Memo, **_):
    annotations = meta.get("annotations", {})
    if annotations.get(ALIAS_ANNOTATION, "").lower() != "true":
        return

    target = annotations.get(TARGET_ANNOTATION) or None
    messages = []
    for host in _ingress_hosts(spec):
        try:
            _, message = dns_alias.reconcile_alias(
                memo.opnsense, memo.dns_config, namespace, "Ingress", name, host, target
            )
        except dns_alias.DNSPolicyError as exc:
            logger.warning(f"refusing alias for {host}: {exc}")
            # patch.metadata.annotations is a property with no setter - individual keys must be
            # assigned into it, a wholesale reassignment raises AttributeError (confirmed
            # 2026-09-16 - the resulting handler failure then retried indefinitely, which is what
            # surfaced the idle= race above).
            patch.metadata.annotations[STATUS_ANNOTATION] = f"conflict: {exc}"
            return
        logger.info(message)
        messages.append(message)

    patch.metadata.annotations[STATUS_ANNOTATION] = (
        ("ready: " + "; ".join(messages)) if messages else "no-hosts"
    )


@kopf.on.delete("networking.k8s.io", "v1", "ingresses")
def delete_ingress(spec, meta, namespace, name, logger, memo: kopf.Memo, **_):
    annotations = meta.get("annotations", {})
    if annotations.get(ALIAS_ANNOTATION, "").lower() != "true":
        return
    for host in _ingress_hosts(spec):
        result = dns_alias.delete_alias(memo.opnsense, namespace, "Ingress", name, host)
        logger.info(f"delete {host}: {result}")


@kopf.on.create("dns.rarosalion.github.io", "v1", "dnsaliases")
@kopf.on.update("dns.rarosalion.github.io", "v1", "dnsaliases")
@kopf.timer("dns.rarosalion.github.io", "v1", "dnsaliases", interval=_RESYNC_INTERVAL, idle=30)
def reconcile_dnsalias(spec, namespace, name, patch, logger, memo: kopf.Memo, **_):
    fqdn = f"{spec['hostname']}.{spec['domain']}"
    target = spec.get("target") or None

    try:
        _, message = dns_alias.reconcile_alias(
            memo.opnsense, memo.dns_config, namespace, "DNSAlias", name, fqdn, target
        )
    except dns_alias.DNSPolicyError as exc:
        patch.status["ready"] = False
        patch.status["message"] = str(exc)
        logger.warning(str(exc))
        return

    patch.status["ready"] = True
    patch.status["message"] = message


@kopf.on.delete("dns.rarosalion.github.io", "v1", "dnsaliases")
def delete_dnsalias(spec, namespace, name, logger, memo: kopf.Memo, **_):
    fqdn = f"{spec['hostname']}.{spec['domain']}"
    result = dns_alias.delete_alias(memo.opnsense, namespace, "DNSAlias", name, fqdn)
    logger.info(f"delete {fqdn}: {result}")
