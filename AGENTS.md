# dns-operator

Kubernetes operator (kopf-based) that creates/manages OPNsense/Unbound DNS *aliases* - never new
host overrides - from `DNSAlias` custom resources and annotated `Ingress` resources. See
`README.md` for the design rationale.

## Layout

- `operator/handlers.py` - kopf handlers: `Ingress` (annotation-gated) and `DNSAlias` create/update/
  delete, plus a 10-minute resync timer on both for drift correction.
- `operator/dns_alias.py` - policy layer: domain/target allowlist validation, the ownership-tag
  scheme (including the opt-in `adopt_unmanaged` path for claiming a pre-existing alias that
  already points at the requested target), and the shared `reconcile_alias`/`delete_alias`
  functions both handler types call.
- `operator/opnsense.py` - the OPNsense API client (`unbound/settings/*HostAlias`,
  `*HostOverride`, `unbound/service/reconfigure`).
- `chart/` - the Helm chart. `chart/crds/` is installed automatically by `helm install`/`upgrade`
  and is *not* removed by `helm uninstall` (standard Helm CRD behavior).
- `examples/` - shape of a `DNSAlias` request and an annotated `Ingress`.
- Modules under `operator/` use flat imports (`import dns_alias`, not `from . import dns_alias`) -
  no `__init__.py`. kopf's `run` command puts the target file's own directory on `sys.path`, so this
  is the layout kopf itself expects for a multi-file operator; a relative/package import would break
  under `kopf run --standalone operator/handlers.py`.

## IMPORTANT: the OPNsense API field shapes here are unverified

`operator/opnsense.py`'s `add_alias`/`set_alias` payloads (the `host_alias` object with `enabled`/
`host`/`hostname`/`domain`/`description` fields) are inferred from OPNsense's general Unbound
plugin conventions (matching how `host_override` objects behave, confirmed live via
`searchHostOverride`) - **not confirmed against a live `addHostAlias`/`setHostAlias` call**, since no
tool exposing those specific actions was available while scaffolding this repo (only
`delHostAlias` was). Before relying on this operator:

1. Run it locally (see "Local dev workflow" below) against a real OPNsense instance.
2. Create a throwaway `DNSAlias` in a domain that's safe to experiment in, and confirm
   `add_alias`/`find_alias` actually round-trip - check the OPNsense UI (Services > Unbound DNS >
   General, under the parent override's alias list) and the operator's own logs.
3. If the field names are wrong, the OPNsense API returns a 400 with a body describing the expected
   shape - fix `opnsense.py` from that, not by guessing again.

## Local dev workflow

Run the operator directly against a real cluster without building a container:

```
export OPNSENSE_HOST=https://opnsense.example.com
export OPNSENSE_API_KEY=... OPNSENSE_API_SECRET=...
export DNS_ALLOWED_DOMAINS=apps.example.internal
export DNS_ALLOWED_TARGETS=ingress.apps.example.internal
export DNS_DEFAULT_TARGET=ingress.apps.example.internal
kopf run --standalone operator/handlers.py
```

`startup()` falls back to `config.load_kube_config()` when not running in-cluster, so this picks up
whatever kubectl context is currently active.

To test the actual container + Helm chart path (recommended before considering a change done - RBAC
issues in particular only show up this way, not via raw `kopf run`):

```
docker build -t dns-operator:local-dev .
helm install dns-operator ./chart \
  --set image.repository=dns-operator --set image.tag=local-dev \
  --set opnsense.host=... --set opnsense.apiKey=... --set opnsense.apiSecret=...
```

## Known gotchas (learned the hard way, or inherited from postgresql-operator)

- **The `ClusterRole` needs `list`/`watch` on `CustomResourceDefinitions`.** kopf's cluster-wide
  resource discovery requires this. Without it, the operator logs repeated 403s in the background,
  eventually gives up, and silently stops watching for CR/Ingress changes (only refreshing on pod
  restart). It still looks like it's "working" at first glance because the initial reconcile
  succeeds before the retries are exhausted.
- **`@kopf.on.create`/`@kopf.on.update` only fire on watch events**, not automatically on operator
  restart for objects that already exist - that's what the `@kopf.timer` resync (every 10 minutes)
  is for here, unlike `postgresql-operator` which doesn't need one.
- **`adopt_unmanaged` only claims a record, it never repoints one.** If an unmanaged alias exists
  with the same hostname/domain but a *different* parent override than the one requested, that's
  still a hard conflict even with adoption on - see the `existing["_parent_uuid"] == parent["uuid"]`
  check in `reconcile_alias`. Don't loosen that without discussing it first; it's the line between
  "claim a record that already agrees with us" and "silently repoint someone else's DNS entry".
- **`patch.metadata.annotations` on Ingress reconciles overwrites the whole annotations dict in the
  patch**, not just `alias-status` - kopf merges patches at the field level, so this is safe, but
  don't casually copy that assignment pattern into a handler that needs to set multiple annotation
  keys across different code paths without re-reading `annotations` first.
- Unlike `postgresql-operator`'s deliberate lack of a delete handler (dropping a database is
  destructive), this operator *does* delete on CR/annotation removal - a DNS alias is cheap and
  reversible, and leaving stale aliases around indefinitely defeats the point of the ownership
  tagging.
