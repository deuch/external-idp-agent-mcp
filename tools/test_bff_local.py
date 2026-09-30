"""Local tests of the Python BFF (BFF_MODE=code), without Azure.

Starts the fake OIDC issuer (tools/fake_idp.py, with RFC 8693 token exchange) and a fake Foundry agent
that echoes what it receives, then drives the BFF in-process. Checks the same contract as the APIM
policies up to the call to the agent: rejected identity headers, token A validation, body allow-list,
OBO exchange and validation of B, derived session, delegation headers, CORS, rate limits.

Run:  .\\.venv-dev\\Scripts\\python tools\\test_bff_local.py
Deps: pip install -r platform/src/bff/requirements.txt starlette
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "platform" / "src" / "bff")]

IDP, AGENT_PORT, WEB = "http://localhost:9000", 9100, "http://localhost:3000"
os.environ.update({
    "OIDC_ISSUER": f"{IDP}/",
    "OIDC_JWKS_URI": f"{IDP}/.well-known/jwks.json",
    "APP_CLIENT_ID": "weather-mobile",
    "BFF_CLIENT_ID": "weather-bff",
    "BFF_CLIENT_SECRET": "local-secret",
    "MCP_AUDIENCE": "weather-mcp",
    "AGENT_ENDPOINT": f"http://127.0.0.1:{AGENT_PORT}",
    "WEB_ORIGIN": WEB,
})
os.environ.pop("AZURE_CLIENT_ID", None)

import fake_idp  # noqa: E402
from azure.core.credentials import AccessToken  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as bff  # noqa: E402
import logging  # noqa: E402

for _name in ("httpx", "httpx2", "bff", "bff.tokens"):
    logging.getLogger(_name).setLevel(logging.ERROR)
from tokens import OboExchanger  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"[{'OK' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}", flush=True)


# ---------------------------------------------------------------- fake Foundry agent
async def fake_responses(request: Request) -> JSONResponse:
    body = await request.json()
    if body.get("previous_response_id") == "resp_unknown":
        return JSONResponse({"error": {"code": "not_found", "message": "Response not found."}}, status_code=404)
    return JSONResponse({
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "agent_session_id": body.get("agent_session_id"),
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "echo": {
            "authorization": request.headers.get("authorization"),
            "identity": request.headers.get("x-ms-user-identity"),
            "mcp_token": request.headers.get("x-client-mcp-token"),
            "api_version": request.query_params.get("api-version"),
            "body": body,
        },
    })


fake_agent = Starlette(routes=[Route("/responses", fake_responses, methods=["POST"])])


def serve(asgi_app, port: int) -> None:
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(f"Port {port} is already in use")
    server = uvicorn.Server(uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return
        time.sleep(0.05)
    raise SystemExit(f"Server on port {port} did not start")


class FakeCredential:
    async def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        return AccessToken("fake-entra-token", int(time.time()) + 3600)

    async def close(self) -> None:
        pass


def mint(**params: str) -> str:
    return httpx.get(f"{IDP}/mint", params=params).text


def claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def app_token(sub: str, **extra: str) -> str:
    return mint(**{"aud": "weather-bff", "azp": "weather-mobile", "scope": "openid profile", "sub": sub, **extra})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- run
serve(fake_idp.app, 9000)
serve(fake_agent, AGENT_PORT)

with TestClient(bff.app) as client:
    client.app.state.agent._credential = FakeCredential()
    settings = client.app.state.settings

    check("health", client.get("/health").status_code == 200)

    # ------------------------------------------------ token A
    r = client.get("/chat/me")
    check("no token -> 401", r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer", str(r.status_code))
    r = client.get("/chat/me", headers=bearer(mint(aud="weather-mcp", azp="weather-mobile", sub="alice")))
    check("wrong audience -> 401", r.status_code == 401, str(r.status_code))
    r = client.get("/chat/me", headers=bearer(mint(aud="weather-bff", azp="other-app", sub="alice")))
    check("wrong azp (token issued to another app) -> 401", r.status_code == 401, str(r.status_code))
    alice = app_token("alice")
    h, p, s = alice.split(".")
    forged = base64.urlsafe_b64encode(json.dumps(dict(claims(alice), sub="bob")).encode()).decode().rstrip("=")
    r = client.get("/chat/me", headers=bearer(f"{h}.{forged}.{s}"))
    check("tampered token (sub changed) -> 401", r.status_code == 401, str(r.status_code))
    r = client.get("/chat/me", headers=bearer(mint(aud="weather-bff", azp="weather-mobile", sub="alice", ttl="-120")))
    check("expired token -> 401", r.status_code == 401, str(r.status_code))

    r = client.get("/chat/me", headers=bearer(alice))
    me = r.json()
    expected_identity = "oidc:" + hashlib.sha256(f"{IDP}/|alice".encode()).hexdigest()
    check("/me returns the validated identity", r.status_code == 200 and me.get("sub") == "alice", str(me))
    check("delegated identity = oidc:sha256(iss|sub)", me.get("delegated_identity") == expected_identity)

    # ------------------------------------------------ identity headers set by the client
    for name in ("x-ms-user-identity", "x-client-mcp-token", "X-Agent-User-Id"):
        r = client.post("/chat/responses", headers={**bearer(alice), name: "x"}, json={"input": "hi"})
        check(f"client-supplied {name} -> 400", r.status_code == 400 and r.json().get("error") == "forbidden_header",
              str(r.status_code))
    r = client.post("/chat/responses", headers={"x-ms-user-identity": "x"}, json={"input": "hi"})
    check("identity header rejected before authentication (same order as APIM)", r.status_code == 400, str(r.status_code))

    # ------------------------------------------------ body allow-list
    for label, raw in [
        ("invalid JSON", b"{not json"),
        ("missing input", json.dumps({"message": "hi"}).encode()),
        ("non-string input", json.dumps({"input": [{"role": "user"}]}).encode()),
        ("empty input", json.dumps({"input": "   "}).encode()),
        ("input > 4000 chars", json.dumps({"input": "x" * 4001}).encode()),
        ("malformed previous_response_id", json.dumps({"input": "hi", "previous_response_id": "../x"}).encode()),
        ("client-supplied agent_session_id", json.dumps({"input": "hi", "agent_session_id": "abc"}).encode()),
    ]:
        r = client.post("/chat/responses", headers={**bearer(alice), "Content-Type": "application/json"}, content=raw)
        check(f"invalid body ({label}) -> 400", r.status_code == 400 and r.json().get("error") == "invalid_request",
              str(r.status_code))

    # ------------------------------------------------ OBO exchange + delegation
    r = client.post("/chat/responses", headers=bearer(alice),
                    json={"input": "Quel temps ?", "tools": [{"type": "web_search"}], "instructions": "x", "model": "y"})
    data = r.json()
    echo = data.get("echo", {})
    check("chat relayed to the agent", r.status_code == 200, str(r.status_code))
    check("agent called with the Entra token of the BFF identity", echo.get("authorization") == "Bearer fake-entra-token")
    check("x-ms-user-identity = delegated identity", echo.get("identity") == expected_identity)
    b = claims(echo.get("mcp_token") or "e30.e30.")
    check("token B: aud = weather-mcp, azp = weather-bff, same sub",
          b.get("aud") == "weather-mcp" and b.get("azp") == "weather-bff" and b.get("sub") == "alice", str(b))
    check("token B never in the body sent to the agent", "eyJ" not in json.dumps(echo.get("body")))
    expected_session = hashlib.sha256(f"session|{expected_identity}".encode()).hexdigest()
    check("body rebuilt: only input + derived agent_session_id (tools/instructions/model stripped)",
          echo.get("body") == {"input": "Quel temps ?", "agent_session_id": expected_session}, str(echo.get("body")))
    check("Responses endpoint called with api-version=v1", echo.get("api_version") == "v1")

    r2 = client.post("/chat/responses", headers=bearer(alice), json={"input": "encore", "previous_response_id": data["id"]})
    echo2 = r2.json().get("echo", {})
    check("previous_response_id forwarded", echo2.get("body", {}).get("previous_response_id") == data["id"])
    check("token B served from cache (same token for the same token A)", echo2.get("mcp_token") == echo.get("mcp_token"))

    r = client.post("/chat/responses", headers=bearer(alice), json={"input": "hi", "previous_response_id": "resp_unknown"})
    check("agent status relayed unchanged (404)", r.status_code == 404, str(r.status_code))

    bob = app_token("bob")
    r = client.post("/chat/responses", headers=bearer(bob), json={"input": "hi"})
    bob_session = r.json().get("echo", {}).get("body", {}).get("agent_session_id")
    check("bob gets his own session", r.status_code == 200 and bob_session not in (None, expected_session))

    # ------------------------------------------------ exchange failures (fail closed, no IdP detail)
    real_exchanger = client.app.state.exchanger
    http = real_exchanger._http
    client.app.state.exchanger = OboExchanger(http, settings, client_secret="wrong-secret")
    r = client.post("/chat/responses", headers=bearer(app_token("carol")), json={"input": "hi"})
    check("exchange refused by the IdP -> 502 generic", r.status_code == 502 and r.json() == {"error": "token_exchange_failed"},
          f"{r.status_code} {r.text}")
    client.app.state.exchanger = OboExchanger(http, settings, scopes="favorites:write")
    r = client.post("/chat/responses", headers=bearer(app_token("dave")), json={"input": "hi"})
    check("token B without weather:read -> 403", r.status_code == 403 and r.json().get("error") == "insufficient_scope",
          f"{r.status_code} {r.text}")
    client.app.state.exchanger = real_exchanger

    # ------------------------------------------------ CORS
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}
    r = client.options("/chat/responses", headers={"Origin": WEB, **preflight})
    check("CORS: web app origin allowed", r.headers.get("access-control-allow-origin") == WEB, str(r.status_code))
    r = client.options("/chat/responses", headers={"Origin": "https://evil.example", **preflight})
    check("CORS: other origin refused", r.headers.get("access-control-allow-origin") not in ("https://evil.example", "*"),
          str(r.status_code))

    # ------------------------------------------------ per-user rate limit (exact: single process)
    erin = app_token("erin")
    statuses = [client.get("/chat/me", headers=bearer(erin)).status_code for _ in range(32)]
    check("per-user rate limit -> 429 after 30 calls/min", statuses[:30].count(200) == 30 and statuses[30:] == [429, 429],
          f"{statuses.count(200)}x200, {statuses.count(429)}x429")

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
