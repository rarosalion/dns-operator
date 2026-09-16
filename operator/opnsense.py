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

    def __init__(self, base_url, api_key, api_secret, verify_tls=True):
        self.base_url = base_url.rstrip("/")
        self.auth = (api_key, api_secret)
        self.verify_tls = verify_tls

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
        # rowCount is set high enough to return every override (and its child aliases) in one
        # page. This operator only ever manages a handful of records, so real pagination isn't
        # worth the complexity.
        data = self._post(
            "unbound/settings/searchHostOverride", json={"current": 1, "rowCount": 1000}
        )
        return data.get("rows", [])

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
        return self._post("unbound/service/reconfigure")
