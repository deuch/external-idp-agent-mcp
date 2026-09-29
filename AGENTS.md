# Agent instructions

This project was built with the microsoft-foundry skill. Before working on or answering questions about foundry agents, read the microsoft-foundry skill first. If you are in VS Code, read the vscode-microsoft-foundry skill first.

## Project layout

- `weather-agent/` — azd project (Foundry project, model deployment, hosted agent in `src/weather-agent/`). Run azd commands from this folder, always with `-e <environment>`. Foundry infra is ejected in `weather-agent/infra/`: the resource group is only created when it does not exist (`AZURE_USE_EXISTING_RESOURCE_GROUP`); never re-tag an existing resource group.
- `platform/` — Bicep (`infra/main.bicep`), Keycloak image and realm configuration (`keycloak/`), APIM policies that implement the BFF (`apim/`, deployed by `infra/apim-config.bicep`), sources of the MCP server and static web client (`src/`).
- `deploy.ps1` / `teardown.ps1` — deploy or remove one environment (one resource group) from `platform/.env`. ACR remote builds, no local Docker.
- `docs/KEYCLOAK.md` — identity provider (Keycloak / myID) configuration.

## Identity invariants (do not break)

- The only identity provider is Keycloak (myID). The app holds a token for the BFF only (`aud=weather-bff`, `azp=weather-mobile`); the BFF obtains the MCP token by RFC 8693 token exchange (`aud=weather-mcp`, `azp=weather-bff`).
- Never send `tools` in Responses API calls to the agent endpoint; tools live in the hosted agent code.
- The user token reaches the container only via the `x-client-mcp-token` header and is kept in a request-scoped ContextVar. Never log it, put it in prompts/history/tool arguments, or write it to `$HOME`.
- `x-ms-user-identity` is derived server-side from the validated `iss` + `sub`.
- The MCP server accepts only tokens issued to the BFF (`MCP_ALLOWED_AZP`), derives the user from the validated `sub` claim only and checks scopes per tool.
- The BFF is API Management (API `chat`): keep the fragment order (reject identity headers -> validate A -> OBO exchange + validate B -> delegation) and the request body allow-list. Only the dedicated identity `id-apim-chat` may hold the Foundry impersonation role. APIM diagnostics must never log headers or bodies.
- The hosted-agent session (`agent_session_id`) is derived by APIM from the validated identity; never accept it from the client (Foundry does not fence delegated users at session level).
- Secrets live only in Key Vault; never commit them or write them to `platform/.env`.
