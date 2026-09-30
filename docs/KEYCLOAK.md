# Configuration de Keycloak (« myID ») pour le POC

Ce document décrit la configuration du realm Keycloak qui joue le rôle de **myID**, et la façon de la reproduire sur le vrai myID.

La source de vérité est [platform/keycloak/realm-config.json](../platform/keycloak/realm-config.json). [configure_realm.py](../platform/keycloak/configure_realm.py) l'applique sans effet de bord, via l'API REST d'administration, à chaque exécution de `deploy.ps1`.

```
App (weather-mobile) ──PKCE──► Keycloak ──► jeton A (aud=weather-bff, azp=weather-mobile)
App ──Bearer A──► BFF (APIM ou Python, client weather-bff) ──token-exchange A→B──► Keycloak ──► jeton B (aud=weather-mcp, azp=weather-bff)
BFF ──► Foundry (x-client-mcp-token: B) ──► Agent ──Bearer B──► MCP (valide iss, aud, azp, scope → sub)
```

## 1. Realm `weather`

| Paramètre | Valeur | Pourquoi |
|---|---|---|
| Access token lifespan | 300 s | Jetons courts : révoquer A ne révoque pas B, B doit donc expirer vite |
| Revoke refresh token / max reuse | ON / 0 | Rotation des refresh tokens et détection de réutilisation |
| Brute force protection | ON | Protection des comptes |
| Registration | OFF | Comptes créés par l'administration (en réel : ils viennent de myID) |

## 2. Clients

| Client | Type | Réglages | Rôle |
|---|---|---|---|
| `weather-mobile` | **Public** | Standard flow uniquement, **PKCE S256 obligatoire**, redirect URIs = URL de l'application web (`ca-web`) ou Universal / App Links (mobile), aucun secret | L'application mobile ou web qui authentifie l'utilisateur |
| `weather-bff` | **Confidentiel** | Aucun flux interactif, **`standard.token.exchange.enabled = true`**, authentification par secret (POC) | Le BFF, porté par **API Management** (secret lu depuis Key Vault par une named value) ou par le **service Python** (secret injecté depuis Key Vault par Container Apps), seul autorisé à échanger A contre B |
| `weather-mcp` | Confidentiel, aucun flux | Sert uniquement de **cible d'audience** | Le serveur MCP (resource server) |

## 3. Client scopes et audiences

| Client scope | Rattaché à | Mapper | Effet |
|---|---|---|---|
| `weather-bff-audience` | `weather-mobile` (défaut) | Audience → `weather-bff` | A contient `aud=weather-bff`. C'est **obligatoire** : Keycloak refuse l'échange si le client demandeur n'est pas dans l'`aud` du jeton d'origine |
| `weather:read` | `weather-bff` (optionnel) | Audience → `weather-mcp` | Ajouté à B quand le BFF demande `scope=weather:read favorites:write` |
| `favorites:write` | `weather-bff` (optionnel) | Audience → `weather-mcp` + scope mapping vers le rôle `weather-premium` | N'apparaît dans B **que si l'utilisateur a le rôle** `weather-premium` |

Rôles du realm : `weather-basic` (Bob) et `weather-premium` (Alice).

## 4. Règles du Standard Token Exchange (Keycloak ≥ 26.2)

1. Le client qui échange (`weather-bff`) doit être **confidentiel**. Un client public reçoit une erreur 400.
2. Le jeton d'origine doit contenir ce client dans son `aud`.
3. `audience=weather-mcp` doit désigner un client du **même realm**. Le paramètre `resource` (RFC 8707) n'est pas supporté.
4. B porte `azp=weather-bff` et le même `sub` que A. Il n'y a pas de claim `act` : la délégation reste expérimentale dans Keycloak.
5. Par défaut, l'échange peut **ajouter** des scopes optionnels du client demandeur. Ils sont ici cadrés par les rôles. Pour interdire tout scope absent de A, appliquez la client policy `downscope-assertion-grant-enforcer`.

## 5. Contrôles côté applicatif

| Composant | Vérifie |
|---|---|
| BFF (APIM ou Python) | A : signature (JWKS du realm), `iss`, `aud ∋ weather-bff`, **`azp = weather-mobile`**, `exp`. Puis B : même `sub`, **`azp = weather-bff`**, `weather:read` présent |
| Agent | Pré-contrôle de B (signature, `iss`, `aud`, `exp`). Sans jeton valide, il répond 401 avant tout appel au modèle |
| Serveur MCP | B : signature, `iss`, `aud ∋ weather-mcp`, **`azp ∈ MCP_ALLOWED_AZP`** (`weather-bff`), scope par outil. L'identité vient de `sub` |

Le contrôle `azp = weather-bff` côté MCP garantit qu'aucun jeton demandé directement depuis un terminal n'est accepté.

## 6. Tests et comptes

- `alice` (premium) et `bob` (basic) : les mots de passe sont générés par `deploy.ps1` et stockés **uniquement** dans le Key Vault du resource group (`alice-password`, `bob-password`).
- Le test de bout en bout ([tools/e2e_test.py](../tools/e2e_test.py)) active **temporairement** le *password grant* sur `weather-mobile`, puis le désactive.
- La console d'administration est à `https://ca-keycloak.<domaine>/admin` (utilisateur `admin`, secret `kc-admin-password`).

## 7. Transposer au vrai myID (Keycloak)

À demander à l'équipe myID, dans leur realm ou dans un realm dédié :

1. Créer les trois clients du §2 et les client scopes du §3, avec les mappers Audience.
2. Activer le **Standard Token Exchange** sur `weather-bff`. En production, l'authentifier par **`private_key_jwt` ou certificat X.509 (mTLS)** plutôt que par un secret.
3. Fixer les durées de vie (A : 5 à 10 min, B : 5 min) et activer la rotation des refresh tokens.
4. Rendre le token endpoint et la JWKS du realm joignables depuis Azure (BFF, agent, MCP).
5. Côté POC, il suffit de changer l'émetteur (`OIDC_ISSUER`, `OIDC_JWKS_URI`) et le secret de `weather-bff` (secret Key Vault lu par le BFF). Ni le code ni les policies ne changent.

## 8. Dépannage

| Symptôme | Cause probable |
|---|---|
| `token_exchange_failed` (BFF, 502) | `standard.token.exchange.enabled` désactivé, mauvais secret, ou `weather-bff` absent de l'`aud` de A |
| `insufficient_scope` (BFF, 403) | Client scope `weather:read` non rattaché (optionnel) à `weather-bff` |
| `Access token was issued to an unexpected client application` | Le jeton ne vient pas de `weather-mobile` |
| MCP 401 | Jeton A envoyé directement, audience ou `azp` incorrects, ou jeton expiré |
| Login : `Invalid redirect uri` | L'URL de l'application web est absente des redirect URIs de `weather-mobile` (relancer `deploy.ps1`) |
