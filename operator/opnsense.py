import threading
import time

import requests


class OpnsenseError(Exception):
    """Raised when the OPNsense API returns an error or an unexpected response shape."""


class OpnsenseClient:
    """Thin wrapper over the parts of OPNsense's Unbound API this operator needs.

    Talks to the `unbound/settings/*HostAlias` and `*HostOverride` controller actions directly -
    these are not exposed by name-scoped OPNsense automation tools, only by the raw core API. A
    host alias is a distinct object from a host override: it has its own UUID and lives attached to
    a parent override via a `host` field holding the parent's UUID.
    """

    _PAGE_SIZE = 1000

    def __init__(
        self,
        base_url,
        api_key,
        api_secret,
        verify_tls=True,
        allow_insecure_transport=False,
        reconfigure_min_interval=5.0,
    ):
        # Basic auth over plain HTTP puts the API key/secret on the wire in cleartext on every
        # request. Refuse it unless explicitly opted into - a typo'd or copy-pasted http:// host
        # shouldn't silently downgrade the transport that's carrying credentials.
        if not base_url.lower().startswith("https://") and not allow_insecure_transport:
            raise ValueError(
                f"OPNSENSE_HOST {base_url!r} must use https:// "
                "(set OPNSENSE_ALLOW_INSECURE_TRANSPORT=true to override)"
            )
        self.base_url = base_url.rstrip("/")
        self.auth = (api_key, api_secret)
        self.verify_tls = verify_tls
        self._reconfigure_min_interval = reconfigure_min_interval
        self._reconfigure_lock = threading.Lock()
        self._last_reconfigure = None

    def _post(self, path, json=None):
        resp = requests.post(
            f"{self.base_url}/api/{path}",
            auth=self.auth,
            json=json if json is not None else {},
            verify=self.verify_tls,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def list_host_overrides(self):
        # Paginated for real: a single instance managed by this operator should only ever have a
        # handful of records, but silently truncating at one page means an override (and its
        # child aliases) past that point would never be found by find_override/find_alias - which
        # would make ownership/conflict checks blind to it rather than erroring loudly.
        rows = []
        page = 1
        while True:
            data = self._post(
                "unbound/settings/searchHostOverride",
                json={"current": page, "rowCount": self._PAGE_SIZE},
            )
            page_rows = data.get("rows", [])
            rows.extend(page_rows)
            total = data.get("total", len(rows))
            if len(page_rows) < self._PAGE_SIZE or len(rows) >= total:
                return rows
            page += 1

    def list_aliases(self):
        aliases = []
        for override in self.list_host_overrides():
            for child in override.get("_children", []):
                child = dict(child)
                child["_parent_uuid"] = override["uuid"]
                aliases.append(child)
        return aliases

    def find_override(self, hostname, domain):
        for row in self.list_host_overrides():
            if row["hostname"] == hostname and row["domain"] == domain:
                return row
        return None

    def find_alias(self, hostname, domain):
        for alias in self.list_aliases():
            if alias["hostname"] == hostname and alias["domain"] == domain:
                return alias
        return None

    def add_alias(self, parent_uuid, hostname, domain, description):
        # The wrapper key is "alias", not "host_alias" - confirmed against a live instance's
        # own unbound/settings/getHostAlias schema (2026-09-16). Field names themselves
        # (enabled/host/hostname/domain/description) were correct on the first guess.
        result = self._post(
            "unbound/settings/addHostAlias",
            json={
                "alias": {
                    "enabled": "1",
                    "host": parent_uuid,
                    "hostname": hostname,
                    "domain": domain,
                    "description": description,
                }
            },
        )
        if result.get("result") != "saved":
            raise OpnsenseError(f"addHostAlias failed: {result}")
        return result

    def set_alias(self, uuid, parent_uuid, hostname, domain, description):
        result = self._post(
            f"unbound/settings/setHostAlias/{uuid}",
            json={
                "alias": {
                    "enabled": "1",
                    "host": parent_uuid,
                    "hostname": hostname,
                    "domain": domain,
                    "description": description,
                }
            },
        )
        if result.get("result") != "saved":
            raise OpnsenseError(f"setHostAlias failed: {result}")
        return result

    def delete_alias(self, uuid):
        result = self._post(f"unbound/settings/delHostAlias/{uuid}")
        if result.get("result") != "deleted":
            raise OpnsenseError(f"delHostAlias failed: {result}")
        return result

    def reconfigure(self):
        # Every alias create/update/delete triggers a real Unbound reload here, and any tenant
        # with create rights on Ingress/DNSAlias can trigger one - a burst of churn (e.g. a
        # CI pipeline rolling out many Ingresses at once) would otherwise hammer Unbound with
        # reloads. Rate-limited to at most one call per `reconfigure_min_interval` seconds by
        # blocking callers until their turn, never by skipping - every call here still results in
        # a real reconfigure before returning, so a mutated alias is always applied, just possibly
        # after a short wait.
        with self._reconfigure_lock:
            if self._last_reconfigure is not None:
                wait = self._last_reconfigure + self._reconfigure_min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            result = self._post("unbound/service/reconfigure")
            self._last_reconfigure = time.monotonic()
            return result
