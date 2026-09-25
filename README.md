# dns-operator

Kubernetes operator that manages DNS aliases in an OPNsense/Unbound resolver, from `DNSAlias`
custom resources and annotated `Ingress` resources - instead of creating them by hand in the
OPNsense UI.

## Why

A common pattern for a small cluster is a single ingress VIP (e.g. a MetalLB-assigned address)
exposed as one OPNsense Unbound host override, with every other hostname an app wants really just
an alias to that same override. Creating each alias by hand in the OPNsense UI is a manual step
outside the cluster's own reconciliation model, with no record of which app "owns" a given alias.

This operator brings that into Kubernetes' own declarative model, the same way `postgresql-operator`
(a sibling project) does for Postgres databases: an app either adds one annotation to its existing
`Ingress`, or (for the non-Ingress case) creates a small `DNSAlias` resource, and the operator
reconciles the actual alias into existence in OPNsense.

## How it works

- The operator only ever creates **aliases** attached to an existing OPNsense host override - never
  a new host override (A record) itself. This means it can never point a hostname at an IP that
  diverges from the actual ingress VIP; the worst it can do is create the wrong *alias*, not the
  wrong *address*.
- Two ways to request an alias:
  - **Ingress annotation** (the common case): add `dns.rarosalion.github.io/alias: "true"` to an
    `Ingress`, and the operator creates an alias for every `spec.rules[].host` on it. Ingresses
    *without* the annotation are ignored entirely - auto-picking up every Ingress host would be a
    surprising way to create public-facing DNS for something only ever meant to be reached from
    inside the cluster.
  - **`DNSAlias` custom resource** (for anything that isn't backed by an Ingress): see
    `examples/dnsalias.yaml`.
  - Both default to the operator's configured `DNS_DEFAULT_TARGET`, and both accept an explicit
    target instead (an annotation or a `spec.target` respectively) - as long as it's in the
    operator's configured allowlist.
- **Domain and target allowlists**: the operator refuses to create an alias in any domain, or
  pointing at any target override, that isn't explicitly configured (`DNS_ALLOWED_DOMAINS`,
  `DNS_ALLOWED_TARGETS`). A typo'd or malicious CR can't alias something outside those lists.
- **Ownership tagging**: every alias the operator creates gets a fixed description tag
  (`managed-by=dns-operator;owner=<namespace>/<kind>/<name>`). Before creating or updating anything,
  it looks for an existing alias with the same hostname/domain - if one exists and isn't tagged as
  owned by *this* resource, the operator refuses and reports a conflict rather than overwriting it.
  This is what makes it safe to introduce into an OPNsense instance that already has hand-created
  aliases.
- **Adopting a pre-existing alias**: by default, an unmanaged alias blocks the operator entirely (see
  above). Setting `dns.adoptUnmanaged: true` (env `DNS_ADOPT_UNMANAGED`) lets the operator take
  ownership of it instead - but only when it already points at the requested target; an unmanaged
  alias pointing somewhere else is still a conflict; adoption claims a record that already agrees
  with what's being requested, it doesn't repoint one that doesn't. This is off by default because
  adopting a hand-made record changes what happens when the owning resource is later deleted - the
  operator will now delete that alias too, which is worth opting into deliberately.
- **Deleting** a `DNSAlias` or removing the annotation from an Ingress deletes the alias too - but
  only if it's still tagged as owned by that resource. If someone hand-edited it in the meantime,
  the operator leaves it alone rather than deleting something it no longer recognizes as its own.
- A periodic resync (every 10 minutes, via `@kopf.timer`) re-runs the same reconcile for every
  annotated Ingress and `DNSAlias`, so drift (e.g. someone edits the alias by hand in the OPNsense
  UI) gets caught even without a new Kubernetes event.

## OPNsense API key

Create a dedicated local OPNsense user (not an admin account) and generate an API key/secret pair
for it. Grant it only the **Services: Unbound DNS** privilege.

Note that this is the narrowest scope OPNsense's own privilege system offers here: it's a
page-level grant, not scoped to "aliases under this one override" - a leaked key could still edit
*any* Unbound override or alias, not just ones this operator created. That's exactly why the
domain/target allowlists and ownership tagging above exist at the application level: OPNsense's own
RBAC can't enforce "only touch children of this one override", so the operator enforces it itself.

The operator refuses a non-`https://` `OPNSENSE_HOST` by default, since Basic auth (how this key is
sent) puts it on the wire in cleartext over plain HTTP - set `dns.opnsense.allowInsecureTransport`
only for a lab instance you genuinely can't put behind TLS.

Store the key/secret in whatever secret manager your cluster already uses (Vault, Sealed Secrets,
SOPS, etc.). Either wire the rendered value into the chart's `opnsense.apiKey`/`apiSecret` values,
or - to avoid the credential passing through Helm values, `--set` shell history, or CI logs at
all - point `opnsense.existingSecret` at a Secret your secret manager already writes into the
cluster directly, with `key`/`secret` data keys matching what this chart's own Secret would use.

## Kubernetes-side blast radius

The `ClusterRole` in `chart/templates/rbac.yaml` grants `patch` on `Ingress` objects
**cluster-wide** (kopf needs it to write the status annotation and its own finalizer/progress
tracking back onto the Ingresses it manages). A compromised operator pod - not a compromised
OPNsense key, the operator process itself - could therefore rewrite any Ingress in the cluster, not
just ones carrying its alias annotation. The delete handler (and the finalizer it requires) are
filtered to only pre-match Ingresses carrying `dns.rarosalion.github.io/alias: "true"`, so an
unrelated Ingress is never held in `Terminating` by this operator being unreachable - but the RBAC
grant itself is still cluster-wide `patch`, same as `postgresql-operator`'s equivalent trade-off for
its own managed resources. There's no narrower built-in Kubernetes RBAC verb for "patch, but only
objects with this annotation" - if that matters for your threat model, consider an admission policy
(e.g. Kyverno/ValidatingAdmissionPolicy) restricting what this ServiceAccount can patch beyond what
RBAC alone expresses.

## Deploying

```
helm install dns-operator ./chart \
  --set opnsense.host=https://opnsense.example.com \
  --set opnsense.apiKey=... \
  --set opnsense.apiSecret=...
```

Adjust `dns.allowedDomains`, `dns.allowedTargets`, and `dns.defaultTarget` in `chart/values.yaml`
(or via `--set`) to match your own domains and existing host override(s).

Other `opnsense.*` values worth knowing about:

- `existingSecret`: use a Secret your own secret manager already writes instead of
  `apiKey`/`apiSecret` (see above).
- `allowInsecureTransport`: opt-in to a non-`https://` `OPNSENSE_HOST` (see above). Leave `false`
  unless you have a specific reason not to.
- `reconfigureMinIntervalSeconds` (default `5`): every alias create/update/delete triggers a real
  Unbound reload; this caps how often a burst of Ingress/DNSAlias churn can force one, without ever
  skipping a reload a change actually needs.

See `examples/dnsalias.yaml` and `examples/ingress-annotation.yaml` for the two ways to request an
alias.
