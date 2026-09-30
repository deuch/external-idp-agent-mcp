"""Settings of the Python BFF, read once from environment variables (see main.bicep)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    # myID (Keycloak) realm
    oidc_issuer: str = field(default_factory=lambda: _required("OIDC_ISSUER"))
    oidc_jwks_uri: str = field(default_factory=lambda: os.environ.get("OIDC_JWKS_URI", ""))
    # Public client of the mobile / web app: token A must have been issued to it (azp).
    app_client_id: str = field(default_factory=lambda: os.environ.get("APP_CLIENT_ID", "weather-mobile"))
    # Confidential client of the BFF, the only one allowed to perform the token exchange.
    bff_client_id: str = field(default_factory=lambda: os.environ.get("BFF_CLIENT_ID", "weather-bff"))
    bff_client_secret: str = field(default_factory=lambda: _required("BFF_CLIENT_SECRET"))
    mcp_audience: str = field(default_factory=lambda: os.environ.get("MCP_AUDIENCE", "weather-mcp"))
    mcp_scopes: str = field(default_factory=lambda: os.environ.get("MCP_SCOPES", "weather:read favorites:write"))
    mcp_required_scope: str = field(default_factory=lambda: os.environ.get("MCP_REQUIRED_SCOPE", "weather:read"))
    # Foundry hosted agent: {project endpoint}/agents/{agent}/endpoint/protocols/openai
    agent_endpoint: str = field(default_factory=lambda: _required("AGENT_ENDPOINT").rstrip("/"))
    # Header carrying token B to the container: only the x-client-* prefix is forwarded by Foundry.
    mcp_token_header: str = field(default_factory=lambda: os.environ.get("MCP_TOKEN_HEADER", "x-client-mcp-token"))
    # Origin of the web app (CORS).
    web_origin: str = field(default_factory=lambda: _required("WEB_ORIGIN").rstrip("/"))
    # Managed identity of the BFF (user-assigned). Empty = DefaultAzureCredential (local development).
    azure_client_id: str = field(default_factory=lambda: os.environ.get("AZURE_CLIENT_ID", ""))
    # Same limits as the APIM policies (per minute).
    ip_limit_per_minute: int = field(default_factory=lambda: int(os.environ.get("RATE_LIMIT_PER_IP", "120")))
    user_limit_per_minute: int = field(default_factory=lambda: int(os.environ.get("RATE_LIMIT_PER_USER", "30")))
    max_input_chars: int = 4000
    agent_timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.mcp_token_header.lower().startswith("x-client-"):
            raise RuntimeError("MCP_TOKEN_HEADER must start with 'x-client-' (only prefix forwarded to hosted agents)")

    @property
    def jwks_uri(self) -> str:
        return self.oidc_jwks_uri or f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"
