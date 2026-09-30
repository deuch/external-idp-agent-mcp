"""Weather MCP server protected by end-user access tokens issued by Keycloak (myID).

The server is an OAuth 2.1 resource server (MCP authorization spec):
- every request must carry `Authorization: Bearer <access token>`
- the token must be issued by the configured issuer, for the MCP audience, to an allowed
  client (the BFF, which obtains it by On-Behalf-Of token exchange), and carry `weather:read`
- the end-user identity is always taken from the validated `sub` claim, never
  from tool arguments supplied by the LLM
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("mcp-weather")

# Keycloak realm issuer, e.g. https://<host>/realms/<realm>
ISSUER = os.environ["OIDC_ISSUER"]
JWKS_URI = os.getenv("OIDC_JWKS_URI") or f"{ISSUER.rstrip('/')}/protocol/openid-connect/certs"
AUDIENCE = os.environ["MCP_AUDIENCE"]
PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
# Only accept tokens issued to these clients (the BFF, so a token requested directly from a
# device is rejected). Empty = no restriction (local tests only).
ALLOWED_AZP = {c.strip() for c in os.getenv("MCP_ALLOWED_AZP", "").split(",") if c.strip()}
READ_SCOPE = "weather:read"
FAVORITES_SCOPE = "favorites:write"

WEATHER_CODES = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant", 51: "bruine légère", 53: "bruine", 55: "bruine dense",
    61: "pluie légère", 63: "pluie", 65: "forte pluie", 71: "neige légère", 73: "neige", 75: "forte neige",
    80: "averses légères", 81: "averses", 82: "fortes averses", 95: "orage", 96: "orage avec grêle", 99: "orage violent",
}


class OidcTokenVerifier(TokenVerifier):
    """Validates RS256 access tokens against the issuer JWKS."""

    def __init__(self) -> None:
        self._jwks = PyJWKClient(JWKS_URI, cache_keys=True, lifespan=3600)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=AUDIENCE,
                issuer=ISSUER,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("Rejected access token: %s", type(exc).__name__)
            return None

        client_id = claims.get("azp") or claims.get("client_id") or ""
        if ALLOWED_AZP and client_id not in ALLOWED_AZP:
            logger.warning("Rejected access token: azp not allowed")
            return None

        scopes = claims.get("scope", "").split()
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=AUDIENCE,
            subject=claims["sub"],
            claims=claims,
        )


mcp = FastMCP(
    "weather",
    instructions="Outils météo. L'identité de l'utilisateur provient du jeton, jamais des arguments.",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8000")),
    stateless_http=True,
    json_response=True,
    token_verifier=OidcTokenVerifier(),
    auth=AuthSettings(
        issuer_url=ISSUER,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        required_scopes=[READ_SCOPE],
        validate_token_resource=False,  # audience is checked by OidcTokenVerifier
    ),
)

# Per-user state keyed by the validated `sub` claim (in-memory, POC only).
_favorites: dict[str, list[str]] = {}


def _current_user() -> AccessToken:
    token = get_access_token()
    if token is None or not token.subject:
        raise PermissionError("Utilisateur non authentifié")
    return token


def _require_scope(token: AccessToken, scope: str) -> None:
    if scope not in token.scopes:
        raise PermissionError(f"Scope manquant : {scope}")


def _display_name(token: AccessToken) -> str | None:
    claims = token.claims or {}
    for key in ("name", "preferred_username", "email"):
        if claims.get(key):
            return str(claims[key])
    return None


_open_meteo_client: httpx.AsyncClient | None = None


def _open_meteo() -> httpx.AsyncClient:
    """Shared client: keep-alive avoids a TLS handshake per call, and a short connect timeout
    with retries absorbs the intermittent TLS handshake stalls seen from Container Apps egress."""
    global _open_meteo_client
    if _open_meteo_client is None:
        _open_meteo_client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.5),
            transport=httpx.AsyncHTTPTransport(
                retries=3, limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60)
            ),
        )
    return _open_meteo_client


async def _geocode(client: httpx.AsyncClient, city: str) -> dict[str, Any] | None:
    resp = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "fr", "format": "json"},
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    return results[0] if results else None


@mcp.tool()
async def get_weather(city: str) -> dict[str, Any]:
    """Retourne la météo actuelle d'une ville (température, ressenti, vent, humidité, conditions)."""
    user = _current_user()
    _require_scope(user, READ_SCOPE)
    logger.info("get_weather city=%s sub=%s", city, user.subject)

    client = _open_meteo()
    started = time.monotonic()
    step = "geocoding"
    try:
        place = await _geocode(client, city)
        if place is None:
            return {"error": f"Ville introuvable : {city}"}
        step = "forecast"
        resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
                "timezone": "auto",
            },
        )
        resp.raise_for_status()
        current = resp.json()["current"]
    except httpx.HTTPError as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning(
            "get_weather failed city=%s step=%s error=%s status=%s elapsed=%.2fs",
            city, step, type(exc).__name__, status, time.monotonic() - started,
        )
        return {"error": "Service météo temporairement indisponible, réessayez dans quelques instants."}
    logger.info("get_weather ok city=%s elapsed=%.2fs", city, time.monotonic() - started)

    return {
        "city": place["name"],
        "country": place.get("country"),
        "time": current["time"],
        "temperature_c": current["temperature_2m"],
        "feels_like_c": current["apparent_temperature"],
        "humidity_pct": current["relative_humidity_2m"],
        "wind_kmh": current["wind_speed_10m"],
        "conditions": WEATHER_CODES.get(current["weather_code"], f"code {current['weather_code']}"),
        "requested_by": user.subject,
    }


@mcp.tool()
async def whoami() -> dict[str, Any]:
    """Indique l'identité de l'utilisateur telle que vue par le serveur MCP (issue du jeton)."""
    user = _current_user()
    return {
        "sub": user.subject,
        "name": _display_name(user),
        "client_id": user.client_id,
        "scopes": user.scopes,
        "token_expires_at": user.expires_at,
    }


@mcp.tool()
async def add_favorite_city(city: str) -> dict[str, Any]:
    """Ajoute une ville aux favoris de l'utilisateur courant (nécessite le scope favorites:write)."""
    user = _current_user()
    _require_scope(user, FAVORITES_SCOPE)
    favorites = _favorites.setdefault(user.subject, [])
    if city not in favorites:
        favorites.append(city)
    return {"favorites": favorites}


@mcp.tool()
async def list_favorite_cities() -> dict[str, Any]:
    """Liste les villes favorites de l'utilisateur courant."""
    user = _current_user()
    return {"favorites": _favorites.get(user.subject, [])}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
