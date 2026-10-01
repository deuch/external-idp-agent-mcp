# Architecture et flux d'authentification

Cette page explique comment l'identité d'un utilisateur authentifié par **myID (Keycloak)** est propagée jusqu'au **serveur MCP**, en passant par un **BFF** (Backend for Frontend) et un **agent hébergé Microsoft Foundry**. Elle décrit aussi comment les jetons sont protégés à chaque étape.

Le BFF existe en **deux implémentations interchangeables**, choisies par `BFF_MODE` :

| Mode | BFF | Identité dédiée vers Foundry | URL du chat |
|---|---|---|---|
| `apim` (défaut) | API `chat` d'**Azure API Management** (policies XML, aucun code) | `id-apim-chat` | `https://apim-wx-<suffixe>.azure-api.net/chat` |
| `code` | Container App **`ca-bff`** (service **Python** FastAPI) | `id-bff` | `https://ca-bff.<domaine>/chat` |

Les deux respectent le même contrat, appliquent les mêmes contrôles dans le même ordre et passent les mêmes tests. Les §§ 1 à 7 valent pour les deux modes ; le [§ 8](#8-deux-implémentations-du-bff-bff_mode) détaille chaque implémentation, dont l'[architecture du mode `code`](#architecture-du-mode-code).

> Version détaillée (principes, menaces, limites) : [Authentification-principes.docx](Authentification-principes.docx). Configuration de l'IdP : [KEYCLOAK.md](KEYCLOAK.md).

## 1. Le problème à résoudre

- Les utilisateurs sont dans un **IdP externe** (myID, un Keycloak), pas dans Entra ID.
- **Foundry n'accepte que des jetons Entra ID**, et son mode « OAuth identity passthrough » exige des utilisateurs Entra du même tenant.
- Les outils d'un agent référencé **ne peuvent pas être redéfinis dans la requête** (`tools: Not allowed when agent is specified`).
- Pourtant, le serveur MCP doit savoir **quel utilisateur** l'appelle, et pouvoir le **vérifier**.

## 2. Les composants

| Composant | Rôle | Identité portée |
|---|---|---|
| **Application web / mobile** (`ca-web`) | Authentifie l'utilisateur (OIDC Authorization Code + PKCE) et appelle le BFF | Client **public** `weather-mobile`, aucun secret |
| **myID (Keycloak)**, realm `weather` | Émet les jetons, réalise l'échange de jeton (RFC 8693), publie ses clés (JWKS) | Émetteur unique (`iss`) |
| **BFF** : API `chat` d'**API Management** (mode `apim`) **ou** Container App `ca-bff` en **Python** (mode `code`) | Valide le jeton, l'échange, délègue l'identité à Foundry | Client **confidentiel** `weather-bff` + identité managée **dédiée** (`id-apim-chat` ou `id-bff`) |
| **Microsoft Entra ID** | Émet le jeton de service du BFF pour Foundry | — |
| **Passerelle Foundry** | Vérifie les droits du BFF, isole les conversations par utilisateur, filtre les en-têtes | — |
| **Agent hébergé** (`weather-agent`) | Agent Framework : utilise le jeton de l'utilisateur pour appeler le serveur MCP | Identité d'agent (appels au modèle) |
| **Serveur MCP** (`ca-mcp-weather`) | Outils météo et favoris, identifie l'utilisateur à partir du jeton | Resource server `weather-mcp` |

## 3. Schéma de flux

![Schéma de flux d'authentification](diagrams/flux-authentification.png)

1. **Authentification** : l'utilisateur se connecte à myID (Authorization Code + PKCE, navigateur système). L'application reçoit le **jeton A** (`aud = weather-bff`, `azp = weather-mobile`) et un refresh token rotatif. Elle n'obtient **jamais** de jeton pour le serveur MCP.
2. L'application appelle le BFF (`POST /chat/responses`) avec A.
3. **Échange de jeton (On-Behalf-Of)** : le BFF présente A à myID avec les identifiants du client confidentiel `weather-bff` et demande `audience = weather-mcp`. myID renvoie le **jeton B** : même utilisateur (`sub`), `azp = weather-bff`, uniquement les scopes auxquels l'utilisateur a droit. Le BFF met B en cache jusqu'à son expiration.
4. Le BFF obtient auprès d'Entra ID le **jeton E** de son identité managée dédiée : il représente le BFF, pas l'utilisateur.
5. Le BFF appelle l'agent avec :
   - E ;
   - l'**identité déléguée** `x-ms-user-identity = oidc:sha256(iss|sub)` ;
   - le jeton B dans `x-client-mcp-token` ;
   - une **session d'agent dérivée de l'identité**.
6. La passerelle Foundry valide E et le droit d'impersonation, isole la conversation par utilisateur, **retire** `Authorization` et relaie les en-têtes `x-client-*` au conteneur.
7. L'agent pré-contrôle B, puis le présente au serveur MCP. Celui-ci le valide (`aud`, `azp = weather-bff`, scope) et identifie l'utilisateur par `sub`.

Les flèches en pointillés vers myID représentent la récupération des **clés publiques (JWKS)**, mises en cache, qui permettent à chaque composant de vérifier les signatures lui-même.

Le flux est **identique dans les deux modes** ; seul l'exécutant des étapes 2 à 5 change :

| Étape | Mode `apim` | Mode `code` |
|---|---|---|
| 2 · réception | passerelle API Management | `ca-bff` (FastAPI, ingress HTTPS de Container Apps) |
| 3 · échange A → B | policy `send-request` ; secret `weather-bff` en *named value* Key Vault ; cache APIM | `httpx` ; secret `weather-bff` injecté par référence Key Vault (secret Container App) ; cache en mémoire |
| 4 · jeton E | `authentication-managed-identity` (`id-apim-chat`) | `ManagedIdentityCredential` du SDK `azure-identity` (`id-bff`) |
| 5 · appel de l'agent | `forward-request` | `httpx.post({agent}/responses?api-version=v1)` |

## 4. Diagramme de séquence

![Diagramme de séquence d'authentification](diagrams/sequence-authentification.png)

<details>
<summary>Version Mermaid (rendue par GitHub)</summary>

```mermaid
sequenceDiagram
    autonumber
    actor U as Utilisateur
    participant App as App mobile<br/>(client public weather-mobile)
    participant KC as myID = Keycloak<br/>(realm, émetteur unique)
    participant BFF as BFF · API chat<br/>APIM (policies) ou ca-bff (Python)<br/>client confidentiel weather-bff
    participant Entra as Microsoft<br/>Entra ID
    participant GW as Gateway<br/>Foundry
    participant Agent as Agent hébergé
    participant MCP as Serveur MCP<br/>(ressource weather-mcp)

    rect rgb(235, 243, 255)
    Note over U,KC: Phase 1 - Authentification unique chez myID (Authorization Code + PKCE)
    U->>App: Ouvre l'application
    App->>KC: /auth (client_id=weather-mobile, code_challenge S256, scope=openid)
    KC->>U: Page de login myID (navigateur système) + MFA
    U->>KC: Identifiants
    KC-->>App: Code d'autorisation
    App->>KC: /token (code + code_verifier)
    KC-->>App: Jeton A (aud=weather-bff, azp=weather-mobile) + refresh token rotatif
    end

    rect rgb(255, 247, 235)
    Note over U,MCP: Phase 2 - Appel du chat : échange de jeton (OBO) côté serveur puis propagation
    U->>App: "Quel temps fait-il à Lyon ?"
    App->>BFF: POST /chat/responses · Authorization: Bearer A
    Note right of BFF: Refuse les en-têtes d'identité · valide A (JWKS myID) :<br/>iss, aud∋weather-bff, azp=weather-mobile, exp<br/>corps en liste blanche (input, previous_response_id)
    alt B absent du cache
        BFF->>KC: /token grant=token-exchange<br/>subject_token=A · audience=weather-mcp · scope=weather:read favorites:write<br/>(auth client weather-bff : secret en POC, private_key_jwt ou mTLS en production)
        Note right of KC: Standard Token Exchange V2 :<br/>weather-bff confidentiel, ∈ aud de A,<br/>scopes autorisés pour ce client et cet utilisateur
        KC-->>BFF: Jeton B (aud=weather-mcp, azp=weather-bff, même sub, 5 min)
        Note right of BFF: Met B en cache<br/>clé = hash(A) · TTL = exp(B) - 60 s
    end
    Note right of BFF: Vérifie B : même sub que A, azp=weather-bff, weather:read
    BFF->>Entra: Jeton managed identity
    Entra-->>BFF: Jeton E (identité dédiée du BFF : id-apim-chat ou id-bff)
    BFF->>GW: POST /responses · Bearer E<br/>x-ms-user-identity: oidc:sha256(iss|sub) · x-client-mcp-token: B<br/>agent_session_id dérivé de l'identité (jamais fourni par le client)
    Note right of GW: RBAC + UserIdentityImpersonation<br/>isolation par utilisateur
    GW->>Agent: /responses (Authorization retiré, x-client-* transmis)
    Note right of Agent: Pré-contrôle de B (JWKS, aud=weather-mcp, exp), sinon 401<br/>B lié à la requête (ContextVar), jamais dans le prompt
    Agent->>MCP: tools/call get_weather · Authorization: Bearer B
    Note left of MCP: Valide B : iss=realm myID, aud∋weather-mcp,<br/>azp=weather-bff, scope, exp · identité = sub
    MCP-->>Agent: Résultat pour sub
    Agent-->>GW: Réponse
    GW-->>BFF: Réponse
    BFF-->>App: Texte de la réponse (aucun jeton)
    end
```

Source : [diagrams/sequence.mmd](diagrams/sequence.mmd). L'image ci-dessus est générée avec `mmdc -i sequence.mmd -o sequence-authentification.png -c mermaid-config.json`.

</details>

**Phase 1 · Authentification.** L'application génère un secret éphémère (`code_verifier`) dont elle n'envoie que l'empreinte (PKCE). Seul le détenteur du secret peut ensuite échanger le code contre des jetons. Aucun secret n'est embarqué dans l'application, et la saisie des identifiants (et du MFA) se fait chez myID.

**Phase 2 · Appel du chat.** Le BFF (APIM ou `ca-bff`) refuse les en-têtes d'identité fournis par le client, valide A, filtre le corps de la requête et échange A contre B (sauf si B est en cache). Il vérifie B, obtient E, puis appelle l'agent. L'agent attache B aux seuls appels MCP de **cette** requête. Le serveur MCP répond pour l'utilisateur identifié par `sub`. Aucun jeton n'est renvoyé à l'application.

## 5. Les jetons

| Jeton | Émetteur | Audience (`aud`) | Émis à (`azp`) | Circule | Durée | Validé par |
|---|---|---|---|---|---|---|
| **A** – utilisateur | myID | `weather-bff` | `weather-mobile` | App → BFF | 5 min | BFF |
| **B** – utilisateur (OBO) | myID (échange) | `weather-mcp` | `weather-bff` | BFF → Foundry → agent → MCP | 5 min | BFF, agent, MCP |
| **E** – service | Entra ID | Foundry | BFF (`id-apim-chat` ou `id-bff`) | BFF → Foundry | géré par Azure | Passerelle Foundry |
| Refresh token | myID | — | `weather-mobile` | reste sur l'app | rotatif | myID |

**Un jeton, une audience** : A n'est jamais transmis au serveur MCP (qui le refuse), il est **échangé**. Si un jeton fuit, il ne peut pas être rejoué contre une autre API.

## 6. À quoi sert l'échange de jeton (OBO)

Sans échange, il faudrait soit transmettre le jeton de l'application au serveur MCP (*token passthrough*, interdit par la spécification MCP), soit que l'application obtienne elle-même un jeton pour le serveur MCP. L'échange côté serveur apporte trois garanties :

1. Le jeton destiné au MCP **n'existe jamais sur le terminal** : un téléphone volé ou une application compromise ne peut pas appeler le serveur MCP.
2. **Seul le BFF peut l'obtenir** : l'échange exige le secret du client confidentiel `weather-bff`, stocké dans Key Vault et lu uniquement par l'identité du BFF (`id-apim-kv` en mode `apim`, `id-bff` en mode `code`). Un client public reçoit une erreur.
3. Le serveur MCP **exige `azp = weather-bff`** : tout jeton obtenu par un autre chemin est rejeté.

Règles du *Standard Token Exchange* de Keycloak (≥ 26.2) utilisées ici :

- le client demandeur doit être **confidentiel** et figurer dans l'`aud` du jeton présenté (mapper Audience `weather-bff` sur `weather-mobile`) ;
- l'`audience` demandée doit être un client du même realm (`weather-mcp`) ;
- les scopes de B viennent des **client scopes optionnels** de `weather-bff`, et `favorites:write` n'est accordé qu'au rôle `weather-premium` ;
- révoquer A ne révoque pas B, d'où la durée de vie courte de B.

## 7. Contrôles à chaque étape

### Le BFF (API Management ou service Python)

Implémentation : policies dans [platform/apim](../platform/apim) (mode `apim`) ou service Python dans [platform/src/bff](../platform/src/bff) (mode `code`), voir [§ 8](#8-deux-implémentations-du-bff-bff_mode). Chaque étape échoue **de façon fermée** : une erreur arrête la requête avant l'appel à l'agent.

| Étape | Contrôle | Échec |
|---|---|---|
| CORS | Seule l'origine de l'application web est autorisée | refus navigateur |
| Limite par IP | 120 requêtes / minute avant authentification | 429 |
| En-têtes d'identité | `x-ms-user-identity`, `x-client-*`, `x-agent-*` envoyés par le client sont **rejetés** | 400 |
| Jeton A (`validate-jwt`) | Signature (JWKS), `iss`, `aud = weather-bff`, `azp = weather-mobile`, expiration | 401 |
| Limite par utilisateur | 30 requêtes / minute par `sub` | 429 |
| Corps en liste blanche | Seuls `input` (1 à 4000 caractères) et `previous_response_id` sont acceptés ; le corps est **reconstruit** (pas de `tools`, `instructions`, `model`, `agent_session_id`) | 400 |
| Échange OBO | A → B avec le secret de `weather-bff` (lu dans Key Vault), mise en cache par empreinte de A | 502 générique |
| Jeton B | Signature, `aud = weather-mcp`, `azp = weather-bff`, même `sub` que A, scope `weather:read` | 502 / 403 |
| Délégation | Jeton Entra de l'identité **dédiée**, identité déléguée, `x-client-mcp-token`, session dérivée de l'identité | — |

Journalisation : en mode `apim`, Application Insights reçoit métriques et erreurs **sans en-têtes ni corps** ; en mode `code`, le service écrit sur la console de Container Apps (Log Analytics) le type d'erreur, un préfixe de l'identité déléguée (opaque), le statut et la latence, et le journal d'accès d'uvicorn ne contient que la méthode, le chemin et le statut. Aucun jeton ne peut s'y retrouver.

### Agent hébergé

- Le jeton B est lu dans `x-client-mcp-token`, **pré-contrôlé** (signature, `aud`, expiration) et conservé dans une `ContextVar` limitée à la requête. Sans jeton valide, l'agent répond **401 avant tout appel au modèle**.
- B n'apparaît **jamais** dans le prompt, l'historique, les arguments des outils, les journaux ou `$HOME` : une injection de prompt ne peut pas l'exfiltrer.
- Le client MCP ajoute `Authorization: Bearer B` à chaque appel. Si l'identité change entre deux requêtes, la connexion MCP est reconstruite.

### Serveur MCP

- Valide B lui-même : signature, `iss`, `aud = weather-mcp`, `azp ∈ MCP_ALLOWED_AZP` (`weather-bff`), expiration.
- L'utilisateur est **toujours** déterminé par le claim `sub`, jamais par un argument que le modèle pourrait remplir.
- Les scopes sont vérifiés **outil par outil** (`weather:read`, `favorites:write`).

## 8. Deux implémentations du BFF (`BFF_MODE`)

Le BFF est choisi dans `platform/.env` : `BFF_MODE="apim"` (défaut) ou `BFF_MODE="code"`. Les deux exposent **le même contrat** (`POST /chat/responses`, `GET /chat/me`, mêmes codes d'erreur), appliquent **les mêmes contrôles dans le même ordre**, et [tools/e2e_test.py](../tools/e2e_test.py) les valide tous les deux. L'application web, Keycloak, l'agent et le serveur MCP sont identiques.

### Architecture du mode `code`

En mode `code`, API Management n'est **pas déployé** : le BFF est une Container App `ca-bff` dans le même environnement Container Apps que Keycloak, le serveur MCP et l'application web.

```mermaid
flowchart LR
    App["App web / mobile<br/>client public weather-mobile"]
    subgraph ACA["Environnement Container Apps"]
        BFF["ca-bff · BFF Python<br/>FastAPI + uvicorn · 1 réplica<br/>identité id-bff"]
        KC["ca-keycloak<br/>myID · realm weather"]
        MCP["ca-mcp-weather<br/>serveur MCP"]
        WEB["ca-web<br/>application web"]
    end
    KV[("Key Vault<br/>weather-bff-client-secret")]
    Entra["Microsoft Entra ID"]
    subgraph Foundry["Microsoft Foundry"]
        GW["Passerelle Foundry"]
        Agent["Agent hébergé<br/>weather-agent"]
    end

    WEB -. sert .-> App
    App -- "1 · login PKCE → jeton A" --> KC
    App -- "2 · POST /chat/responses<br/>Bearer A" --> BFF
    KV -. "secret injecté au démarrage<br/>(référence Key Vault, id-bff)" .-> BFF
    BFF -- "3 · token exchange A → B<br/>client weather-bff" --> KC
    BFF -- "4 · jeton E (id-bff)" --> Entra
    BFF -- "5 · Bearer E · x-ms-user-identity<br/>x-client-mcp-token: B · session" --> GW
    GW --> Agent
    Agent -- "Bearer B" --> MCP
```

**Ressources propres au mode** (créées par [main.bicep](../platform/infra/main.bicep) quand `bffMode = 'code'`) :

| Ressource | Rôle | Remplace en mode `apim` |
|---|---|---|
| Container App `ca-bff` (image `bff:<tag>` construite dans l'ACR) | Le BFF : ingress HTTPS public, port 8080, sondes `/health`, **1 réplica** | service API Management + API `chat` |
| Identité managée `id-bff` | Seule identité du BFF : tire l'image, lit le secret, appelle Foundry | `id-apim-chat` (Foundry) + `id-apim-kv` (Key Vault) |

**Droits de `id-bff`** (moindre privilège) :

| Rôle | Portée | Pourquoi |
|---|---|---|
| AcrPull | registre de conteneurs | tirer l'image `bff` |
| Key Vault Secrets User | Key Vault | lire `weather-bff-client-secret` |
| Foundry Agent Consumer | projet Foundry | appeler l'agent `weather-agent` |
| Foundry Agent User Identity Impersonation (rôle personnalisé) | projet Foundry | déclarer l'identité déléguée `x-ms-user-identity` |

**Secrets.** Le secret du client confidentiel `weather-bff` reste dans Key Vault. La Container App le référence (`keyVaultUrl` + `id-bff`) et le reçoit dans la variable `BFF_CLIENT_SECRET` ; il n'apparaît ni dans l'image, ni dans `platform/.env`, ni dans les sorties de déploiement. Les autres paramètres (`OIDC_ISSUER`, `OIDC_JWKS_URI`, `APP_CLIENT_ID`, `BFF_CLIENT_ID`, `MCP_AUDIENCE`, `MCP_SCOPES`, `AGENT_ENDPOINT`, `WEB_ORIGIN`) sont des variables d'environnement non sensibles.

**Organisation du code** ([platform/src/bff](../platform/src/bff), environ 500 lignes) :

| Module | Contenu |
|---|---|
| [settings.py](../platform/src/bff/settings.py) | configuration lue dans l'environnement (le service refuse de démarrer s'il manque un paramètre obligatoire) |
| [security.py](../platform/src/bff/security.py) | refus des en-têtes d'identité, limiteur à fenêtre glissante (IP et utilisateur) |
| [tokens.py](../platform/src/bff/tokens.py) | validation de A (JWKS mis en cache), identité déléguée, **échange OBO + cache**, validation de B |
| [foundry.py](../platform/src/bff/foundry.py) | session dérivée de l'identité, appel de l'agent avec le jeton E (`ManagedIdentityCredential`, scope `https://ai.azure.com/.default`) |
| [app.py](../platform/src/bff/app.py) | routes FastAPI, CORS, enchaînement des contrôles, liste blanche du corps |

**Limites propres au mode.** Le cache des jetons B et les compteurs de débit sont **en mémoire** : le BFF tourne avec un seul réplica pour que les limites soient exactes. Pour monter en charge, partager cache et compteurs (Azure Cache for Redis) avant d'augmenter `maxReplicas`. Un redémarrage vide le cache : le premier appel de chaque utilisateur refait simplement l'échange.

**Tester sans Azure.** [tools/test_bff_local.py](../tools/test_bff_local.py) démarre un faux IdP et un faux agent, puis vérifie les 35 contrôles du BFF Python en local.

**Déployer.** Avec `BFF_MODE="code"` dans `platform/.env`, `deploy.ps1` construit l'image `bff`, déploie `ca-bff` et `id-bff` au lieu d'API Management, saute l'étape 6b (configuration de l'API APIM), puis lance les mêmes tests de bout en bout contre `https://ca-bff.<domaine>/chat`. L'application web reçoit cette URL comme URL du chat.

### Correspondance policy APIM ↔ code Python

| Étape | API Management ([platform/apim](../platform/apim)) | Python ([platform/src/bff](../platform/src/bff)) |
|---|---|---|
| CORS | `<cors>` dans [api-chat.xml](../platform/apim/api-chat.xml) | `CORSMiddleware` dans [app.py](../platform/src/bff/app.py) |
| Limites par IP et par utilisateur | `rate-limit-by-key` dans [api-chat.xml](../platform/apim/api-chat.xml) | `SlidingWindowLimiter` ([security.py](../platform/src/bff/security.py)), appelé par `authenticated_user` ([app.py](../platform/src/bff/app.py)) |
| Refus des en-têtes d'identité | [reject-sensitive-headers.xml](../platform/apim/fragments/reject-sensitive-headers.xml) | `has_forbidden_header` ([security.py](../platform/src/bff/security.py)) |
| Validation de A | `validate-jwt` dans [validate-app-token.xml](../platform/apim/fragments/validate-app-token.xml) | `validate_app_token` ([tokens.py](../platform/src/bff/tokens.py)) |
| Identité déléguée | variable `delegatedIdentity` ([validate-app-token.xml](../platform/apim/fragments/validate-app-token.xml)) | `delegated_identity` ([tokens.py](../platform/src/bff/tokens.py)) |
| Corps en liste blanche | [op-responses.xml](../platform/apim/op-responses.xml) | `parse_chat_body` ([app.py](../platform/src/bff/app.py)) |
| **Échange OBO + cache** | `send-request`, `cache-lookup-value` / `cache-store-value` ([obo-exchange.xml](../platform/apim/fragments/obo-exchange.xml)) | **`OboExchanger.exchange`** ([tokens.py](../platform/src/bff/tokens.py)) |
| Validation de B | `validate-jwt token-value` + contrôle `sub` / scope ([obo-exchange.xml](../platform/apim/fragments/obo-exchange.xml)) | `validate_mcp_token` ([tokens.py](../platform/src/bff/tokens.py)) |
| Jeton Entra + délégation | [foundry-delegation.xml](../platform/apim/fragments/foundry-delegation.xml) | `FoundryAgentClient.respond` ([foundry.py](../platform/src/bff/foundry.py)) |
| Session dérivée de l'identité | [op-responses.xml](../platform/apim/op-responses.xml) | `session_for` ([foundry.py](../platform/src/bff/foundry.py)) |
| Appel de l'agent | `rewrite-uri` + `forward-request` | `POST {agent}/responses?api-version=v1` ([foundry.py](../platform/src/bff/foundry.py)), réponse relayée telle quelle |

### L'échange de jeton en Python

C'est une simple requête HTTP vers le token endpoint de myID ([tokens.py](../platform/src/bff/tokens.py)) :

```python
resp = await http.post(token_endpoint, data={
    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",   # RFC 8693
    "subject_token": token_a,                                          # jeton de l'application
    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    "audience": "weather-mcp",                                         # ressource visée
    "scope": "weather:read favorites:write",                           # accordés selon les rôles
    "client_id": "weather-bff",                                        # client confidentiel
    "client_secret": secret,                                           # lu dans Key Vault
})
token_b = resp.json()["access_token"]   # aud=weather-mcp, azp=weather-bff, même sub
```

Le BFF met ensuite B en cache (clé : empreinte SHA-256 de A, jusqu'à 60 s avant son expiration), puis **revalide B à chaque requête** : signature, `aud`, `azp = weather-bff`, même `sub` que A, scope `weather:read`.

### Différences entre les deux modes

| | `apim` | `code` |
|---|---|---|
| Code applicatif | aucun (policies XML) | ~500 lignes Python lisibles |
| Identité appelant Foundry | `id-apim-chat` (dédiée) ; `id-apim-kv` lit Key Vault | `id-bff` (dédiée, lit aussi Key Vault) |
| Limitation de débit | approximative (compteurs APIM v2 asynchrones) | exacte (un seul réplica ; Redis pour passer à l'échelle) |
| Tests | bout en bout uniquement | [tests locaux sans Azure](../tools/test_bff_local.py) + bout en bout |
| Déploiement | plus long et plus coûteux (service API Management) | plus rapide et moins coûteux (une Container App) |
| Extensions possibles | gouvernance, quotas, AI Gateway, catalogue d'API | logique métier, DPoP, tests unitaires |

**Un environnement = un mode** : `deploy.ps1` refuse de changer le mode d'un environnement existant. Sinon, l'identité de l'ancien mode conserverait son droit d'impersonation. Utilisez un autre environnement azd / resource group, ou supprimez d'abord l'environnement avec `teardown.ps1`.

## 9. Identité déléguée et sessions Foundry

- `x-ms-user-identity = oidc:` + SHA-256(`iss` | `sub`) : un identifiant **opaque et stable**, calculé par le BFF à partir du jeton validé. Foundry s'en sert pour **isoler les conversations** de chaque utilisateur. Seule l'identité dédiée du BFF (`id-apim-chat` ou `id-bff`) possède la permission `UserIdentityImpersonation` (rôle personnalisé).
- **Constat du test de sécurité** : Foundry isole les *conversations* entre utilisateurs délégués (le `previous_response_id` d'un autre utilisateur renvoie 404), mais **pas les sessions** (le bac à sable du conteneur, avec son `$HOME`). C'est pourquoi le BFF **rejette tout `agent_session_id` fourni par le client** et impose une session par utilisateur : `SHA-256("session|" + identité déléguée)`.

## 10. Menaces et contre-mesures (résumé)

| Menace | Contre-mesure |
|---|---|
| Vol de jeton sur le terminal | PKCE, pas de secret embarqué, jetons courts, rotation des refresh tokens ; le terminal n'a jamais de jeton pour le MCP |
| Rejeu de A contre le MCP | Audiences distinctes : le MCP refuse A |
| Obtention de B par un autre client | Échange réservé au client confidentiel ; le MCP exige `azp = weather-bff` |
| Usurpation via les en-têtes d'identité | Rejetés par le BFF ; recalculés à partir du jeton validé |
| Choix de la session d'un autre utilisateur | Session imposée par le BFF, dérivée de l'identité |
| Injection de champs Responses (`tools`, `instructions`…) | Corps reconstruit à partir d'une liste blanche |
| Injection de prompt (exfiltration, usurpation) | Le modèle ne voit aucun jeton ; l'identité vient du `sub` validé |
| Fuite dans les journaux | Aucun en-tête ni corps journalisé ; jetons jamais écrits |
| Jeton forgé ou expiré | Rejet par le BFF, l'agent et le serveur MCP |

Ces cas sont vérifiés par [tools/e2e_test.py](../tools/e2e_test.py) (31 tests) à chaque déploiement, dans les deux modes du BFF.

## 11. Limites et compromis du POC

- Le jeton B **transite par la passerelle Foundry**, en en-tête, sans persistance dans l'historique.
- La **confiance dans le BFF est centrale** (APIM ou service Python) : il échange les jetons et déclare l'identité à Foundry. D'où l'identité dédiée, le code ou les policies versionnés et le test de sécurité à chaque déploiement.
- Keycloak n'ajoute pas de claim `act` : la chaîne de délégation se reconstitue par `azp` et par les journaux de myID.
- La limitation de débit d'APIM v2 est **approximative** (compteurs synchronisés de façon asynchrone) ; celle du BFF Python est exacte mais locale au réplica.
- Tout est **public** pour le POC. En production, prévoir Front Door + WAF devant le BFF (APIM ou `ca-bff`), des backends privés, Keycloak en haute disponibilité, et l'authentification de `weather-bff` par `private_key_jwt` ou mTLS.
