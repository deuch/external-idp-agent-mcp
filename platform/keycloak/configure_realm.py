"""Idempotently applies realm-config.json to a Keycloak server through the Admin REST API.

Environment:
  KC_URL              https://<keycloak host>
  KC_ADMIN_USER       admin (master realm)
  KC_ADMIN_PASSWORD   from Key Vault (never written to disk)
  WEB_URL, BFF_CLIENT_SECRET, ALICE_PASSWORD, BOB_PASSWORD   placeholders of realm-config.json

Usage:
  python configure_realm.py apply                      # create / update everything
  python configure_realm.py export <file>              # partial export (secrets masked by Keycloak)
  python configure_realm.py direct-grant on|off        # temporarily allow ROPC on weather-mobile (tests only)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

CONFIG_FILE = Path(__file__).with_name("realm-config.json")


def resolve(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise SystemExit(f"Missing environment variable {name}")
            return os.environ[name]
        return re.sub(r"\$\{([A-Z0-9_]+)\}", repl, value)
    if isinstance(value, list):
        return [resolve(v) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items() if not k.startswith("$")}
    return value


class Admin:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=30)
        resp = self.http.post(
            f"{self.base}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": user, "password": password},
        )
        resp.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = self.http.request(method, f"{self.base}/admin{path}", **kw)
        if resp.status_code >= 400 and not (method == "GET" and resp.status_code == 404):
            raise SystemExit(f"{method} {path} -> {resp.status_code} {resp.text[:300]}")
        return resp


def apply(admin: Admin, cfg: dict[str, Any]) -> None:
    realm_rep = cfg["realm"]
    r = realm_rep["realm"]
    if admin.req("GET", f"/realms/{r}").status_code == 404:
        admin.req("POST", "/realms", json=realm_rep)
        print(f"realm {r}: created")
    else:
        admin.req("PUT", f"/realms/{r}", json=realm_rep)
        print(f"realm {r}: updated")
    rp = f"/realms/{r}"

    for role in cfg["roles"]:
        if admin.req("GET", f"{rp}/roles/{role['name']}").status_code == 404:
            admin.req("POST", f"{rp}/roles", json=role)
        else:
            admin.req("PUT", f"{rp}/roles/{role['name']}", json=role)
        print(f"role {role['name']}: ok")

    client_ids: dict[str, str] = {}
    for client in cfg["clients"]:
        found = admin.req("GET", f"{rp}/clients", params={"clientId": client["clientId"]}).json()
        if found:
            cid = found[0]["id"]
            admin.req("PUT", f"{rp}/clients/{cid}", json={**found[0], **client})
            print(f"client {client['clientId']}: updated")
        else:
            admin.req("POST", f"{rp}/clients", json=client)
            cid = admin.req("GET", f"{rp}/clients", params={"clientId": client["clientId"]}).json()[0]["id"]
            print(f"client {client['clientId']}: created")
        client_ids[client["clientId"]] = cid

    existing_scopes = {s["name"]: s["id"] for s in admin.req("GET", f"{rp}/client-scopes").json()}
    for scope in cfg["clientScopes"]:
        rep = {
            "name": scope["name"],
            "description": scope.get("description", ""),
            "protocol": "openid-connect",
            "attributes": {
                "include.in.token.scope": str(scope.get("includeInTokenScope", True)).lower(),
                "display.on.consent.screen": "false",
            },
        }
        if scope["name"] in existing_scopes:
            sid = existing_scopes[scope["name"]]
            admin.req("PUT", f"{rp}/client-scopes/{sid}", json={**rep, "id": sid})
        else:
            admin.req("POST", f"{rp}/client-scopes", json=rep)
            sid = {s["name"]: s["id"] for s in admin.req("GET", f"{rp}/client-scopes").json()}[scope["name"]]

        mapper = {
            "name": f"audience-{scope['audience']}",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": scope["audience"],
                "id.token.claim": "false",
                "access.token.claim": "true",
                "introspection.token.claim": "true",
            },
        }
        mappers = admin.req("GET", f"{rp}/client-scopes/{sid}/protocol-mappers/models").json()
        current = next((m for m in mappers if m["name"] == mapper["name"]), None)
        if current:
            admin.req("PUT", f"{rp}/client-scopes/{sid}/protocol-mappers/models/{current['id']}", json={**mapper, "id": current["id"]})
        else:
            admin.req("POST", f"{rp}/client-scopes/{sid}/protocol-mappers/models", json=mapper)

        # A client scope with role scope mappings is only granted to users holding one of those roles.
        wanted_roles = scope.get("requiredRealmRoles", [])
        mapped = admin.req("GET", f"{rp}/client-scopes/{sid}/scope-mappings/realm").json()
        to_add = [admin.req("GET", f"{rp}/roles/{n}").json() for n in wanted_roles if n not in {m['name'] for m in mapped}]
        if to_add:
            admin.req("POST", f"{rp}/client-scopes/{sid}/scope-mappings/realm", json=to_add)

        for client_id in scope.get("defaultFor", []):
            admin.req("PUT", f"{rp}/clients/{client_ids[client_id]}/default-client-scopes/{sid}")
        for client_id in scope.get("optionalFor", []):
            admin.req("PUT", f"{rp}/clients/{client_ids[client_id]}/optional-client-scopes/{sid}")
        print(f"client scope {scope['name']}: ok (aud={scope['audience']})")

    for user in cfg["users"]:
        rep = {k: user[k] for k in ("username", "email", "firstName", "lastName")}
        rep.update({"enabled": True, "emailVerified": True, "requiredActions": []})
        found = admin.req("GET", f"{rp}/users", params={"username": user["username"], "exact": "true"}).json()
        if found:
            uid = found[0]["id"]
            admin.req("PUT", f"{rp}/users/{uid}", json=rep)
        else:
            admin.req("POST", f"{rp}/users", json=rep)
            uid = admin.req("GET", f"{rp}/users", params={"username": user["username"], "exact": "true"}).json()[0]["id"]
        admin.req("PUT", f"{rp}/users/{uid}/reset-password", json={"type": "password", "value": user["password"], "temporary": False})
        roles = [admin.req("GET", f"{rp}/roles/{n}").json() for n in user.get("realmRoles", [])]
        if roles:
            admin.req("POST", f"{rp}/users/{uid}/role-mappings/realm", json=roles)
        print(f"user {user['username']}: ok")


def set_direct_grant(admin: Admin, cfg: dict[str, Any], enabled: bool) -> None:
    rp = f"/realms/{cfg['realm']['realm']}"
    client = admin.req("GET", f"{rp}/clients", params={"clientId": "weather-mobile"}).json()[0]
    client["directAccessGrantsEnabled"] = enabled
    admin.req("PUT", f"{rp}/clients/{client['id']}", json=client)
    print(f"weather-mobile directAccessGrantsEnabled={enabled}")


def export(admin: Admin, cfg: dict[str, Any], target: str) -> None:
    rp = f"/realms/{cfg['realm']['realm']}"
    data = admin.req("POST", f"{rp}/partial-export", params={"exportClients": "true", "exportGroupsAndRoles": "true"}).json()
    Path(target).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"realm exported to {target} (Keycloak masks client secrets)")


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "apply"
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    admin = Admin(os.environ["KC_URL"], os.getenv("KC_ADMIN_USER", "admin"), os.environ["KC_ADMIN_PASSWORD"])
    if action == "apply":
        apply(admin, resolve(raw))
    elif action == "export":
        export(admin, raw, sys.argv[2])
    elif action == "direct-grant":
        set_direct_grant(admin, raw, sys.argv[2] == "on")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
