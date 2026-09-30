"""Python BFF of the weather chat — same API contract as the APIM "chat" API (BFF_MODE=code).

    POST /chat/responses   chat message to the hosted agent (Responses API format)
    GET  /chat/me          identity of the caller as validated by the BFF (claims only, never a token)
    GET  /health           probes

Every request goes through the same steps, in the same order, as the APIM policies:
    CORS -> per-IP limit -> identity headers rejected -> token A validated -> per-user limit
    -> body allow-list -> OBO exchange A -> B -> validation of B -> delegation to the Foundry agent
Every step fails closed: an error stops the request before the agent is called.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from foundry import FoundryAgentClient
from security import SlidingWindowLimiter, has_forbidden_header
from settings import Settings
from tokens import (
    ExchangeFailed,
    InsufficientScope,
    OboExchanger,
    OidcValidator,
    TokenRejected,
    delegated_identity,
    validate_app_token,
    validate_mcp_token,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for noisy in ("httpx", "httpx2", "httpcore", "azure.identity", "azure.core.pipeline.policies.http_logging_policy"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("bff")

RESPONSE_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str | None = None) -> None:
        self.status, self.error, self.message = status, error, message


def parse_chat_body(raw: bytes, max_chars: int) -> tuple[str, str | None] | None:
    """Body allow-list (equivalent of op-responses.xml): only `input` and `previous_response_id`.

    The body sent to the agent is rebuilt from these fields: the client can never send tools,
    instructions, model... nor choose the hosted-agent session (agent_session_id is rejected).
    """
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or "agent_session_id" in body:
        return None
    text = body.get("input")
    if not isinstance(text, str) or not (1 <= len(text.strip()) <= max_chars):
        return None
    previous = body.get("previous_response_id")
    if previous is not None and (not isinstance(previous, str) or not RESPONSE_ID.fullmatch(previous)):
        return None
    return text.strip(), previous


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(timeout=15)
        credential = (ManagedIdentityCredential(client_id=settings.azure_client_id)
                      if settings.azure_client_id else DefaultAzureCredential())
        app.state.validator = OidcValidator(settings)
        app.state.exchanger = OboExchanger(http, settings)
        app.state.agent = FoundryAgentClient(http, credential, settings)
        app.state.ip_limiter = SlidingWindowLimiter()
        app.state.user_limiter = SlidingWindowLimiter()
        logger.info("BFF started (issuer=%s, agent=%s)", settings.oidc_issuer, settings.agent_endpoint)
        try:
            yield
        finally:
            await credential.close()
            await http.aclose()

    app = FastAPI(title="Weather chat BFF (Python)", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    # CORS: only the web app origin (equivalent of the <cors> policy).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=600,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.error}
        if exc.message:
            body["message"] = exc.message
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
        if exc.status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse(body, status_code=exc.status, headers=headers)

    async def authenticated_user(request: Request) -> tuple[str, dict[str, Any]]:
        """Equivalent of api-chat.xml: applies to every /chat operation."""
        state = request.app.state
        client_ip = request.client.host if request.client else "unknown"
        if not await state.ip_limiter.allow(f"ip-{client_ip}", settings.ip_limit_per_minute):
            raise ApiError(429, "rate_limited")
        if has_forbidden_header(request.headers.keys()):
            raise ApiError(400, "forbidden_header", "Identity headers cannot be set by the client.")
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            raise ApiError(401, "unauthorized")
        token = authorization[7:].strip()
        try:
            claims = await validate_app_token(state.validator, settings, token)
        except TokenRejected:
            raise ApiError(401, "unauthorized") from None
        if not await state.user_limiter.allow(f"sub-{claims['sub']}", settings.user_limit_per_minute):
            raise ApiError(429, "rate_limited")
        return token, claims

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/chat/me")
    async def me(user: tuple[str, dict[str, Any]] = Depends(authenticated_user)) -> dict[str, Any]:
        _, claims = user
        return {
            "sub": claims["sub"],
            "delegated_identity": delegated_identity(claims),
            "scopes": [s for s in claims.get("scope", "").split() if s],
            "expires_at": datetime.fromtimestamp(claims["exp"], tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    @app.post("/chat/responses")
    async def responses(request: Request, user: tuple[str, dict[str, Any]] = Depends(authenticated_user)) -> Response:
        token_a, claims = user
        state = request.app.state
        parsed = parse_chat_body(await request.body(), settings.max_input_chars)
        if parsed is None:
            raise ApiError(400, "invalid_request",
                           'Expected {"input": string (1-4000 chars), "previous_response_id"?}. '
                           "The session is managed by the gateway.")
        text, previous_response_id = parsed

        # On-Behalf-Of token exchange (equivalent of fragments/obo-exchange.xml).
        try:
            token_b = await state.exchanger.exchange(token_a)
            await validate_mcp_token(state.validator, settings, token_b, claims)
        except ExchangeFailed:
            raise ApiError(502, "token_exchange_failed") from None
        except InsufficientScope:
            raise ApiError(403, "insufficient_scope", "The user is not allowed to use the weather service.") from None

        # Delegation to the Foundry hosted agent (equivalent of fragments/foundry-delegation.xml).
        identity = delegated_identity(claims)
        started = time.perf_counter()
        try:
            result = await state.agent.respond(text=text, previous_response_id=previous_response_id,
                                               identity=identity, mcp_token=token_b)
        except Exception as exc:  # noqa: BLE001 - never leak internal details
            logger.warning("Agent call failed: %s", type(exc).__name__)
            raise ApiError(502, "agent_unavailable") from None
        logger.info("chat user=%s status=%s latency_ms=%d", identity[:16], result.status_code,
                    (time.perf_counter() - started) * 1000)
        return Response(content=result.content, status_code=result.status_code, media_type=result.content_type)

    return app


app = create_app()
