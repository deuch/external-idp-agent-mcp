"""Token handling — equivalent of the APIM policies:
  - fragments/validate-app-token.xml : validation of token A + delegated identity
  - fragments/obo-exchange.xml       : On-Behalf-Of token exchange A -> B (RFC 8693) + validation of B

Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from settings import Settings

logger = logging.getLogger("bff.tokens")


class TokenRejected(Exception):
    """Token A is missing or invalid (-> 401)."""


class ExchangeFailed(Exception):
    """The token exchange failed or returned an invalid token B (-> 502)."""


class InsufficientScope(Exception):
    """Token B does not belong to the user or lacks the required scope (-> 403)."""


class OidcValidator:
    """Validates RS256 access tokens issued by the realm (signature via JWKS, iss, aud, exp)."""

    def __init__(self, settings: Settings) -> None:
        self._issuer = settings.oidc_issuer
        self._jwks = PyJWKClient(settings.jwks_uri, cache_keys=True, lifespan=3600)

    async def decode(self, token: str, audience: str) -> dict[str, Any]:
        key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=self._issuer,
            leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )


async def validate_app_token(validator: OidcValidator, settings: Settings, token: str) -> dict[str, Any]:
    """Token A: aud = BFF client, azp = app client (weather-mobile)."""
    try:
        claims = await validator.decode(token, settings.bff_client_id)
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        logger.info("Token A rejected: %s", type(exc).__name__)
        raise TokenRejected from exc
    if claims.get("azp") != settings.app_client_id or not claims.get("sub"):
        logger.info("Token A rejected: unexpected azp or empty sub")
        raise TokenRejected
    return claims


def delegated_identity(claims: dict[str, Any]) -> str:
    """Opaque, stable end-user identifier sent to Foundry (x-ms-user-identity).

    `sub` is only unique per issuer, hence iss|sub. Same derivation as the APIM policy.
    """
    digest = hashlib.sha256(f"{claims['iss']}|{claims['sub']}".encode()).hexdigest()
    return f"oidc:{digest}"


class OboExchanger:
    """RFC 8693 token exchange (Keycloak Standard Token Exchange) with the confidential BFF client.

    Token B is cached until 60 s before it expires, keyed by a hash of token A (never by the token itself).
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings, *, client_id: str | None = None,
                 client_secret: str | None = None, scopes: str | None = None) -> None:
        self._http = http
        self._settings = settings
        self._client_id = client_id or settings.bff_client_id
        self._client_secret = client_secret or settings.bff_client_secret
        self._scopes = scopes or settings.mcp_scopes
        self._token_endpoint: str | None = None
        self._cache: dict[str, tuple[str, float]] = {}

    async def _endpoint(self) -> str:
        if self._token_endpoint is None:
            resp = await self._http.get(f"{self._settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration")
            resp.raise_for_status()
            self._token_endpoint = resp.json()["token_endpoint"]
        return self._token_endpoint

    async def exchange(self, subject_token: str) -> str:
        cache_key = hashlib.sha256(subject_token.encode()).hexdigest()
        cached = self._cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        try:
            resp = await self._http.post(
                await self._endpoint(),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": subject_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "audience": self._settings.mcp_audience,
                    "scope": self._scopes,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            logger.warning("Token exchange failed: %s", type(exc).__name__)
            raise ExchangeFailed from exc
        if resp.status_code != 200:
            # The IdP response is never returned to the client.
            error = resp.json().get("error") if resp.headers.get("content-type", "").startswith("application/json") else None
            logger.warning("Token exchange failed: HTTP %s %s", resp.status_code, error)
            raise ExchangeFailed
        payload = resp.json()
        token = payload["access_token"]
        ttl = max(30, int(payload.get("expires_in", 300)) - 60)
        self._cache[cache_key] = (token, time.time() + ttl)
        if len(self._cache) > 10_000:
            now = time.time()
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
        return token


async def validate_mcp_token(validator: OidcValidator, settings: Settings, token_b: str,
                             app_claims: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth: B must target the MCP server, be issued to the BFF and belong to the same user."""
    try:
        claims = await validator.decode(token_b, settings.mcp_audience)
    except (jwt.PyJWTError, httpx.HTTPError) as exc:
        logger.warning("Exchanged token rejected: %s", type(exc).__name__)
        raise ExchangeFailed from exc
    if claims.get("azp") != settings.bff_client_id:
        logger.warning("Exchanged token rejected: unexpected azp")
        raise ExchangeFailed
    if claims["sub"] != app_claims["sub"] or settings.mcp_required_scope not in claims.get("scope", "").split():
        raise InsufficientScope
    return claims
