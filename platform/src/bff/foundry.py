"""Call to the Foundry hosted agent — equivalent of the APIM policies:
  - fragments/foundry-delegation.xml : Entra token of the BFF identity, x-ms-user-identity, x-client-mcp-token
  - op-responses.xml (backend part)   : session derived from the identity, rewrite to the Responses endpoint

The agent response (status and body) is relayed unchanged, exactly like APIM does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from settings import Settings

FOUNDRY_SCOPE = "https://ai.azure.com/.default"


def session_for(identity: str) -> str:
    """Hosted-agent session (sandbox) of the user.

    Foundry does not fence delegated users from each other at session level, so the session is never
    chosen by the client: it is derived from the validated identity. Same derivation as the APIM policy.
    """
    return hashlib.sha256(f"session|{identity}".encode()).hexdigest()


@dataclass
class AgentResponse:
    status_code: int
    content: bytes
    content_type: str


class FoundryAgentClient:
    def __init__(self, http: httpx.AsyncClient, credential: AsyncTokenCredential, settings: Settings) -> None:
        self._http = http
        self._credential = credential
        self._settings = settings

    async def respond(self, *, text: str, previous_response_id: str | None, identity: str, mcp_token: str) -> AgentResponse:
        entra = await self._credential.get_token(FOUNDRY_SCOPE)
        body: dict[str, str] = {"input": text, "agent_session_id": session_for(identity)}
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        resp = await self._http.post(
            f"{self._settings.agent_endpoint}/responses",
            params={"api-version": "v1"},
            headers={
                "Authorization": f"Bearer {entra.token}",
                "x-ms-user-identity": identity,
                self._settings.mcp_token_header: mcp_token,
            },
            json=body,
            timeout=self._settings.agent_timeout_seconds,
        )
        return AgentResponse(resp.status_code, resp.content, resp.headers.get("content-type", "application/json"))
