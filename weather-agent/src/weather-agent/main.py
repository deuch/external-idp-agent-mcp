# Copyright (c) Microsoft. All rights reserved.
"""Foundry hosted agent that calls a user-authenticated MCP server.

Identity propagation:
  BFF --(x-client-mcp-token: <user access token, aud=MCP>)--> Foundry gateway --> this container
  this container --(Authorization: Bearer <same token>)--> MCP server

The Foundry gateway never forwards `Authorization` to the container, but forwards every
`x-client-*` header unchanged. The token is kept in a request-scoped ContextVar only: it is
never placed in the prompt, the conversation history, tool arguments, logs or `$HOME`.
"""

import asyncio
import logging
import os
from contextvars import ContextVar
from typing import Any

import jwt
from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from jwt import PyJWKClient
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

load_dotenv()

logger = logging.getLogger("weather-agent")

MCP_TOKEN_HEADER = os.getenv("MCP_TOKEN_HEADER", "x-client-mcp-token").lower().encode()
OIDC_ISSUER = os.getenv("OIDC_ISSUER")
MCP_AUDIENCE = os.getenv("MCP_AUDIENCE")

_user_mcp_token: ContextVar[str | None] = ContextVar("user_mcp_token", default=None)


class _TokenPrecheck:
    """Defense in depth: reject expired/forged tokens before they reach the MCP server.

    The MCP server remains the authoritative validator (signature, audience, scopes).
    """

    def __init__(self) -> None:
        self._jwks = None
        if OIDC_ISSUER and MCP_AUDIENCE:
            jwks_uri = os.getenv("OIDC_JWKS_URI") or f"{OIDC_ISSUER.rstrip('/')}/protocol/openid-connect/certs"
            self._jwks = PyJWKClient(jwks_uri, cache_keys=True, lifespan=3600)

    def is_valid(self, token: str) -> bool:
        if self._jwks is None:
            return True
        try:
            key = self._jwks.get_signing_key_from_jwt(token)
            jwt.decode(token, key.key, algorithms=["RS256"], audience=MCP_AUDIENCE, issuer=OIDC_ISSUER, leeway=30)
            return True
        except jwt.PyJWTError as exc:
            logger.warning("Ignoring invalid user token: %s", type(exc).__name__)
            return False


class UserTokenMiddleware:
    """Pure-ASGI middleware binding the per-request user token to a ContextVar."""

    def __init__(self, app: ASGIApp, precheck: _TokenPrecheck) -> None:
        self.app = app
        self.precheck = precheck

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = None
        for name, value in scope.get("headers", []):
            if name.lower() == MCP_TOKEN_HEADER:
                candidate = value.decode("latin-1").strip()
                if candidate.lower().startswith("bearer "):
                    candidate = candidate[7:].strip()
                if candidate and await asyncio.to_thread(self.precheck.is_valid, candidate):
                    token = candidate
                break

        reset = _user_mcp_token.set(token)
        try:
            if token is None and scope.get("method") == "POST" and scope.get("path", "").rstrip("/").endswith("/responses"):
                # Fail closed: every tool needs the user's identity.
                await JSONResponse(
                    {"error": {"code": "user_token_required", "message": f"Missing or invalid {MCP_TOKEN_HEADER.decode()} header"}},
                    status_code=401,
                )(scope, receive, send)
                return
            await self.app(scope, receive, send)
        finally:
            _user_mcp_token.reset(reset)


def mcp_auth_headers(_: dict[str, Any]) -> dict[str, str]:
    token = _user_mcp_token.get()
    if not token:
        # Tolerated by Agent Framework outside a run (eager connection).
        raise KeyError("No user token bound to the current request")
    return {"Authorization": f"Bearer {token}"}


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    credential = DefaultAzureCredential()

    weather_mcp = MCPStreamableHTTPTool(
        name="weather",
        url=os.environ["MCP_SERVER_URL"],
        description="Météo et favoris de l'utilisateur connecté",
        header_provider=mcp_auth_headers,
        approval_mode="never_require",
        load_prompts=False,
    )

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=credential,
    )

    agent = Agent(
        client=client,
        name="weather-agent",
        instructions=(
            "Tu es un assistant météo concis qui répond en français. "
            "Utilise l'outil get_weather pour toute question météo, et les outils de favoris "
            "pour gérer les villes favorites de l'utilisateur. "
            "N'invente jamais de données météo. Si un outil échoue pour une raison d'authentification, "
            "indique à l'utilisateur de se reconnecter."
        ),
        tools=[weather_mcp],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.add_middleware(UserTokenMiddleware, precheck=_TokenPrecheck())
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
