"""Minimal fake OIDC issuer for local tests (no Keycloak needed).

Serves /.well-known/openid-configuration and /.well-known/jwks.json, and:
- mints RS256 access tokens:  GET /mint?aud=<audience>&sub=<sub>&scope=<scopes>&azp=<client>&ttl=<seconds>
- performs RFC 8693 token exchanges (like Keycloak's Standard Token Exchange) on POST /oauth/token,
  for the confidential client weather-bff / local-secret

Run:  python tools/fake_idp.py   (listens on http://localhost:9000/)
"""

import time
import uuid

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

ISSUER = "http://localhost:9000/"
KID = "local-test-key"
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_jwk = RSAAlgorithm.to_jwk(_key.public_key(), as_dict=True) | {"kid": KID, "use": "sig", "alg": "RS256"}


async def openid_configuration(_: Request) -> JSONResponse:
    return JSONResponse({"issuer": ISSUER, "jwks_uri": f"{ISSUER}.well-known/jwks.json", "token_endpoint": f"{ISSUER}oauth/token"})


async def jwks(_: Request) -> JSONResponse:
    return JSONResponse({"keys": [_jwk]})


async def mint(request: Request) -> PlainTextResponse:
    q = request.query_params
    now = int(time.time())
    claims = {
        "iss": q.get("iss", ISSUER),
        "sub": q.get("sub", "00000000-0000-4000-8000-000000000001"),
        "aud": q.get("aud", "weather-mcp"),
        "azp": q.get("azp", "weather-bff"),
        "scope": q.get("scope", "weather:read"),
        "iat": now,
        "exp": now + int(q.get("ttl", "600")),
        "jti": uuid.uuid4().hex,
        "name": q.get("name", "Local Test User"),
        "preferred_username": q.get("username", "local-user"),
    }
    return PlainTextResponse(jwt.encode(claims, _key, algorithm="RS256", headers={"kid": KID}))


BFF_CLIENT = ("weather-bff", "local-secret")


async def token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:token-exchange":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if (form.get("client_id"), form.get("client_secret")) != BFF_CLIENT:
        return JSONResponse({"error": "unauthorized_client"}, status_code=401)
    try:
        subject = jwt.decode(form["subject_token"], _key.public_key(), algorithms=["RS256"], audience=BFF_CLIENT[0], issuer=ISSUER)
    except jwt.PyJWTError:
        return JSONResponse({"error": "invalid_request", "error_description": "invalid subject_token"}, status_code=400)
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": subject["sub"], "aud": form.get("audience"), "azp": BFF_CLIENT[0],
        "scope": form.get("scope", ""), "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex,
        "name": subject.get("name"), "preferred_username": subject.get("preferred_username"),
    }
    return JSONResponse({
        "access_token": jwt.encode(claims, _key, algorithm="RS256", headers={"kid": KID}),
        "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "token_type": "Bearer", "expires_in": 300,
    })


app = Starlette(routes=[
    Route("/.well-known/openid-configuration", openid_configuration),
    Route("/.well-known/jwks.json", jwks),
    Route("/mint", mint),
    Route("/oauth/token", token, methods=["POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="warning")
