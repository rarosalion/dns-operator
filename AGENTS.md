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
- `requirements.in`/`requirements.txt` - the actual pinned, hash-locked dependency set the
  Dockerfile installs (`pip install --require-hashes -r requirements.txt`). `pyproject.toml`'s
  looser `>=` ranges are for the package spec only; regenerate `requirements.txt` with pip-tools
  (command in its own header comment) after bumping a version in `requirements.in` - don't hand-edit
  the hashes.
- Modules under `operator/` use flat imports (`import dns_alias`, not `from . import dns_alias`) -
  no `__init__.py`. kopf's `run` command puts the target file's own directory on `sys.path`, so this
  is the layout kopf itself expects for a multi-file operator; a relative/package import would break
  under `kopf run --standalone operator/handlers.py`.

## OPNsense API field shapes: verified 2026-09-16

`operator/opnsense.py`'s `add_alias`/`set_alias` payloads were originally guessed (wrapper key
`host_alias`) and confirmed wrong the first time this was actually run against a live instance -
OPNsense returned `{"result": "failed"}` with no validation detail. The real wrapper key is
**`alias`**, discovered from the instance's own `unbound/settings/getHostAlias` (GET, no id) -
OPNsense's "get the empty-record schema" convention for any model, worth reaching for early instead
of guessing again from a failed call with no error detail in the body. The field names
themselves (`enabled`/`host`/`hostname`/`domain`/`description`) were right on the first guess.
If this ever needs re-deriving (a future OPNsense version, a different model), hit that same
`get*`-with-no-id endpoint pattern first.

## Local dev workflow

Run the operator directly against a real cluster without building a container:

```
export OPNSENSE_HOST=https://opnsense.example.com
export OPNSENSE_API_KEY=... OPNSENSE_API_SECRET=...
export DNS_ALLOWED_DOMAINS=apps.example.internal
export DNS_ALLOWED_TARGETS=ingress.apps.example.internal
export DNS_DEFAULT_TARGET=ingress.apps.example.internal
export KUBE_CONTEXT=docker-desktop   # or whatever your local test cluster's context is named
kopf run --standalone operator/handlers.py
```

**Always set `KUBE_CONTEXT` explicitly for a local/dev run - never rely on ambient
current-context.** Confirmed 2026-09-16: in an environment with multiple kubeconfig files merged
via the `KUBECONFIG` env var, `kubectl config current-context` reported one (harmless, empty
local) cluster while the `kubernetes` Python client's `load_kube_config()` with no explicit
`context=` silently resolved to a completely different one - in this case a real production
cluster, watched read-only (no writes occurred, since nothing had the alias annotation) but not
what was intended. The fix is the custom `@kopf.on.login()` handler in `handlers.py`, not a plain
`@kopf.on.startup()` call - see the gotcha below for why that distinction matters.

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
- **`patch.metadata.annotations = {...}` (wholesale reassignment) raises `AttributeError: property
  'annotations' of 'MetaPatch' object has no setter`.** Confirmed 2026-09-16 running the real
  operator: this crashed `reconcile_ingress` on every invocation (including retries), which in turn
  is what surfaced the duplicate-alias race below - the handler kept retrying indefinitely instead
  of ever reaching a clean, settled state. Assign individual keys into the existing proxy object
  instead: `patch.metadata.annotations[STATUS_ANNOTATION] = value`.
- **A `@kopf.timer`'s first firing can race a just-created object's own `on.create`/`on.update`
  handling**, both seeing "no alias exists yet" and both calling `add_alias`, producing a real
  duplicate alias in OPNsense (confirmed 2026-09-16, surfaced by the crash above causing repeated
  retries that widened the race window). Both timers here are declared with `idle=30` specifically
  to prevent this - don't remove it, and don't assume a lower value is safe without testing the
  same race again.
- **kopf has its own built-in login activity (`kopf.login_via_client`) that is completely separate
  from anything a plain `@kopf.on.startup()` handler does.** It's what kopf's actual watch/patch
  traffic authenticates through, and it calls `kubernetes.config.load_kube_config()` with no
  context of its own. Registering a custom `@kopf.on.login()` handler (as `handlers.py` does)
  replaces it entirely - this is the only way to make `KUBE_CONTEXT` actually take effect; setting
  up the k8s client config inside `@kopf.on.startup()` looks like it should work and visibly does
  nothing for kopf's own connection (confirmed 2026-09-16 - an earlier version of this operator
  loaded the context in `startup()` and still watched the wrong cluster).
- Unlike `postgresql-operator`'s deliberate lack of a delete handler (dropping a database is
  destructive), this operator *does* delete on CR/annotation removal - a DNS alias is cheap and
  reversible, and leaving stale aliases around indefinitely defeats the point of the ownership
  tagging.
- **A bare `USER 1000` in the Dockerfile (no matching `/etc/passwd` entry) crash-loops the
  container**, even under `--standalone`: kopf's own peering-identity detection unconditionally
  calls `getpass.getuser()` -> `pwd.getpwuid(os.getuid())`, which raises `KeyError: uid not found`
  for a UID with no passwd entry. Confirmed 2026-09-16 deploying the "restricted" PodSecurity fix to
  the real cluster - the previous working pod had already terminated by the time this surfaced, so
  briefly there were zero running replicas. `useradd` before switching `USER` fixes it - but note
  Debian's default `/etc/group` already has a group literally named `operator` (confirmed via the
  `useradd: group operator exists` error), so name the created user something else (`app`, here) or
  pass an explicit `-g`.
- **The Dockerfile's `pip install kopf>=1.37 kubernetes>=31.0 requests>=2.32` line was silently
  unpinned the whole time.** `RUN` executes under a shell, and `>=1.37` etc. are each parsed as
  output redirection (`>` truncating/creating a file named `=1.37`), not a version constraint -
  confirmed by reproducing it outside Docker (2026-09-25): the command that actually ran was
  `pip install --no-cache-dir kopf kubernetes requests`, fully unpinned, leaving three stray empty
  files behind. Fixed by installing from `requirements.txt` (`--require-hashes`) instead - if a
  future dependency line needs adding directly in a `RUN pip install ...`, quote or escape any `>`
  in it, or better, add it to `requirements.in` and regenerate the lock instead.
- **A `@kopf.on.delete` handler with no `labels`/`annotations`/`when` filter gets a finalizer added
  to *every* object of that resource type kopf can see, cluster-wide** - not just the ones the
  handler's own body logic would actually act on. Confirmed 2026-09-25 against kopf's own
  `registry._changing.requires_finalizer()`: an unfiltered, non-optional `on.delete("...",
  "ingresses")` pre-matches (and thus finalizes) every Ingress in the cluster, including ones that
  never carried the alias annotation - the early `return` inside the handler body only skips *what
  it does*, not whether kopf blocks deletion on it first. If the operator or OPNsense is then
  unreachable, deleting any Ingress (or any namespace containing one) hangs in `Terminating`. Fixed
  by passing the same `annotations={ALIAS_ANNOTATION: <predicate>}` filter used on create/update/
  timer to `on.delete` too (`_INGRESS_ALIAS_FILTER` in `handlers.py`) - kopf's own pre-match, not
  just the handler body, needs to agree with "only opted-in Ingresses".  Don't add a new Ingress
  handler here without the same filter, or this regresses silently (it looks identical in normal
  operation - the difference only shows up when something's unreachable and someone tries to delete
  an unrelated Ingress).
- **The `FROM python:3.12-slim` base image is now pinned by digest**
  (`python:3.12-slim@sha256:...`), resolved 2026-09-25 from the registry's own manifest-list digest
  for that tag (`GET /v2/library/python/manifests/3.12-slim` against `registry-1.docker.io`, with an
  `Accept: application/vnd.oci.image.index.v1+json` header - this is the multi-arch list digest, not
  one platform's own manifest digest, so it still resolves correctly on both amd64 and arm64). A tag
  alone is mutable and gives no record of what actually got built. Renovate's default Docker manager
  handles refreshing this automatically; to redo it by hand, repeat that same registry query rather
  than trusting `docker pull`'s locally-cached digest, which can be stale.
- **`cluster-management-talos`'s `helmfile apply` has shown two distinct, unreliable-caching
  failure modes here** (both confirmed 2026-09-16-17, root cause not fully pinned down): (1) a
  fresh install under `atomic: true` + `wait: true` created every resource then silently rolled all
  of it back with no visible error, and a subsequent `helmfile apply` then reported "no diff" even
  though nothing existed on the cluster; (2) after clearing the chart cache
  (`helmfile cache cleanup`), an upgrade that `helmfile diff` correctly showed as changed reported
  "Release has been upgraded... REVISION: 2" without the Deployment's `.metadata.generation` or
  image actually changing on the cluster - `helm history` still showed only revision 1. Both times,
  a plain `helm upgrade --install ... --set ...` (bypassing helmfile, values reconstructed from
  `values.yaml.gotmpl` by hand) produced a real, verifiable change immediately. If a `helmfile
  apply` here ever looks like it succeeded, verify independently
  (`kubectl get deploy -o jsonpath='{.metadata.generation}'` and the running image) rather than
  trusting its own "Upgrade complete" output - and reach for plain `helm` directly if it disagrees.
