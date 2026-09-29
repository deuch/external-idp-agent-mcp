// Platform for the weather chat POC:
//   Keycloak ("myID") + Azure API Management as the BFF (OBO token exchange in policies)
//   + static web app + MCP server, on Azure Container Apps.
// The Foundry project and hosted agent are provisioned by azd (weather-agent/).
//
// Three stages (see deploy.ps1):
//   base    : registry, logs, App Insights, Container Apps environment, Key Vault, identities, APIM service
//   apps    : PostgreSQL, Keycloak, MCP server, web app, Foundry role assignments
//   gateway : apps + APIM configuration. Runs after the Keycloak realm exists, because APIM downloads
//             the OpenID configuration of the realm when it saves the validate-jwt policies.
targetScope = 'resourceGroup'

param location string = resourceGroup().location

@allowed(['base', 'apps', 'gateway'])
param stage string = 'apps'

@description('Object id of the user running the deployment (Key Vault Secrets Officer, to seed the secrets)')
param deployerObjectId string

param foundryAccountName string
param foundryProjectName string
param foundryProjectEndpoint string
param agentName string = 'weather-agent'

param keycloakImage string = ''
param mcpImage string = ''
param webImage string = ''

@secure()
param dbAdminPassword string = ''

@allowed(['BasicV2', 'StandardV2'])
param apimSku string = 'BasicV2'
param apimPublisherEmail string = 'noreply@example.com'

param realm string = 'weather'
param appClientId string = 'weather-mobile'
param bffClientId string = 'weather-bff'
param mcpAudience string = 'weather-mcp'
param mcpScopes string = 'weather:read favorites:write'

var suffix = uniqueString(resourceGroup().id)
var tags = { project: 'external-idp-agent-mcp', stack: 'keycloak-obo-apim' }
var deployApps = stage == 'apps' || stage == 'gateway'
var deployGateway = stage == 'gateway'
var kcAppName = 'ca-keycloak'
var mcpAppName = 'ca-mcp-weather'
var webAppName = 'ca-web'
var dbAdmin = 'kcadmin'

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  foundryAgentConsumer: 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

// ---------------------------------------------------------------- base
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrwx${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-wx-${suffix}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-wx-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id }
}

resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-wx-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-wx-${take(suffix, 13)}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource deployerSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, deployerObjectId, roles.keyVaultSecretsOfficer)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
  }
}

resource kcIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-keycloak-${suffix}'
  location: location
  tags: tags
}

resource mcpIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-mcp-weather-${suffix}'
  location: location
  tags: tags
}

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-web-${suffix}'
  location: location
  tags: tags
}

// APIM identities: one to read Key Vault secrets (named values), one DEDICATED to the chat API.
// Only the chat identity may call the agent and delegate the end-user identity to Foundry.
resource apimKvIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-apim-kv-${suffix}'
  location: location
  tags: tags
}

resource apimChatIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-apim-chat-${suffix}'
  location: location
  tags: tags
}

resource kcAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, kcIdentity.id, roles.acrPull)
  properties: {
    principalId: kcIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource mcpAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, mcpIdentity.id, roles.acrPull)
  properties: {
    principalId: mcpIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource webAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, webIdentity.id, roles.acrPull)
  properties: {
    principalId: webIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource kcVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, kcIdentity.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: kcIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

resource apimVaultReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, apimKvIdentity.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: apimKvIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

// Public gateway (POC). The service is created in the base stage because it is the slowest resource.
resource apim 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: 'apim-wx-${suffix}'
  location: location
  tags: tags
  sku: { name: apimSku, capacity: 1 }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${apimKvIdentity.id}': {}, '${apimChatIdentity.id}': {} }
  }
  properties: {
    publisherEmail: apimPublisherEmail
    publisherName: 'Weather POC'
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------- apps
var domain = acaEnv.properties.defaultDomain
var kcUrl = 'https://${kcAppName}.${domain}'
var issuer = '${kcUrl}/realms/${realm}'
var jwksUri = '${issuer}/protocol/openid-connect/certs'
var mcpUrl = 'https://${mcpAppName}.${domain}'
var webUrl = 'https://${webAppName}.${domain}'
var chatApiUrl = '${apim.properties.gatewayUrl}/chat'

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (deployApps) {
  name: 'psql-wx-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: dbAdmin
    administratorLoginPassword: dbAdminPassword
    storage: { storageSizeGB: 32, autoGrow: 'Enabled' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
  }
}

resource keycloakDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (deployApps) {
  parent: postgres
  name: 'keycloak'
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

// POC: the Container Apps environment is not VNet-integrated, so allow Azure services.
resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (deployApps) {
  parent: postgres
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

resource keycloakApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: kcAppName
  location: location
  tags: union(tags, { role: 'identity-provider' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${kcIdentity.id}': {} } }
  dependsOn: [ kcAcrPull, kcVaultReader, keycloakDb, pgFirewall ]
  properties: {
    environmentId: acaEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'auto', allowInsecure: false }
      registries: [ { server: registry.properties.loginServer, identity: kcIdentity.id } ]
      secrets: [
        { name: 'kc-admin-password', keyVaultUrl: '${vault.properties.vaultUri}secrets/kc-admin-password', identity: kcIdentity.id }
        { name: 'kc-db-password', keyVaultUrl: '${vault.properties.vaultUri}secrets/kc-db-password', identity: kcIdentity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'keycloak'
          image: keycloakImage
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'KC_DB_URL', value: 'jdbc:postgresql://${postgres!.properties.fullyQualifiedDomainName}:5432/keycloak?sslmode=require' }
            { name: 'KC_DB_USERNAME', value: dbAdmin }
            { name: 'KC_DB_PASSWORD', secretRef: 'kc-db-password' }
            { name: 'KC_HOSTNAME', value: kcUrl }
            { name: 'KC_HTTP_ENABLED', value: 'true' }
            { name: 'KC_PROXY_HEADERS', value: 'xforwarded' }
            { name: 'KC_CACHE', value: 'local' }
            { name: 'KC_BOOTSTRAP_ADMIN_USERNAME', value: 'admin' }
            { name: 'KC_BOOTSTRAP_ADMIN_PASSWORD', secretRef: 'kc-admin-password' }
          ]
          probes: [
            { type: 'Startup', httpGet: { path: '/health/started', port: 9000 }, periodSeconds: 10, failureThreshold: 30, initialDelaySeconds: 10 }
            { type: 'Liveness', httpGet: { path: '/health/live', port: 9000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health/ready', port: 9000 }, periodSeconds: 15 }
          ]
        }
      ]
      // Single node for the POC (local cache). Use AKS + Keycloak Operator for HA.
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

resource mcpApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: mcpAppName
  location: location
  tags: union(tags, { role: 'mcp-server' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${mcpIdentity.id}': {} } }
  dependsOn: [ mcpAcrPull ]
  properties: {
    environmentId: acaEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8000, transport: 'auto', allowInsecure: false }
      registries: [ { server: registry.properties.loginServer, identity: mcpIdentity.id } ]
    }
    template: {
      containers: [
        {
          name: 'mcp-weather'
          image: mcpImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'OIDC_ISSUER', value: issuer }
            { name: 'OIDC_JWKS_URI', value: jwksUri }
            { name: 'MCP_AUDIENCE', value: mcpAudience }
            { name: 'MCP_ALLOWED_AZP', value: bffClientId }
            { name: 'MCP_PUBLIC_URL', value: mcpUrl }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// Static web client (simulates the mobile app). Runtime configuration and CSP come from env vars.
resource webApp 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: webAppName
  location: location
  tags: union(tags, { role: 'web-client' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${webIdentity.id}': {} } }
  dependsOn: [ webAcrPull ]
  properties: {
    environmentId: acaEnv.id
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'auto', allowInsecure: false }
      registries: [ { server: registry.properties.loginServer, identity: webIdentity.id } ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'OIDC_AUTHORITY', value: issuer }
            { name: 'APP_CLIENT_ID', value: appClientId }
            { name: 'CHAT_API_URL', value: chatApiUrl }
            { name: 'IDP_ORIGIN', value: kcUrl }
            { name: 'API_ORIGIN', value: apim.properties.gatewayUrl }
          ]
          probes: [
            { type: 'Liveness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 30 }
            { type: 'Readiness', httpGet: { path: '/health', port: 8080 }, periodSeconds: 10 }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
}

// Required to send `x-ms-user-identity` (delegated end-user identity). Not part of any built-in role.
resource impersonationRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (deployApps) {
  name: guid(resourceGroup().id, 'foundry-agent-user-identity-impersonation')
  properties: {
    roleName: 'Foundry Agent User Identity Impersonation (${resourceGroup().name})'
    description: 'Lets a trusted middle-tier service delegate the end-user identity to a hosted agent via the x-ms-user-identity header.'
    type: 'CustomRole'
    assignableScopes: [ resourceGroup().id ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [ 'Microsoft.CognitiveServices/accounts/AIServices/agents/endpoints/UserIdentityImpersonation/action' ]
        notDataActions: []
      }
    ]
  }
}

resource chatAgentConsumer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApps) {
  scope: foundryProject
  name: guid(foundryProject.id, apimChatIdentity.id, roles.foundryAgentConsumer)
  properties: {
    principalId: apimChatIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.foundryAgentConsumer)
  }
}

resource chatImpersonation 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApps) {
  scope: foundryProject
  name: guid(foundryProject.id, apimChatIdentity.id, 'user-identity-impersonation')
  properties: {
    principalId: apimChatIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: impersonationRole.id
  }
}

module apimConfig 'apim-config.bicep' = if (deployGateway) {
  name: 'apim-config'
  dependsOn: [ apimVaultReader, chatAgentConsumer, chatImpersonation ]
  params: {
    apimName: apim.name
    appInsightsName: appInsights.name
    keyVaultUri: vault.properties.vaultUri
    kvIdentityClientId: apimKvIdentity.properties.clientId
    chatIdentityClientId: apimChatIdentity.properties.clientId
    oidcIssuer: issuer
    appClientId: appClientId
    bffClientId: bffClientId
    mcpAudience: mcpAudience
    mcpScopes: mcpScopes
    webOrigin: webUrl
    agentEndpoint: '${foundryProjectEndpoint}/agents/${agentName}/endpoint/protocols/openai'
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output keyVaultName string = vault.name
output apimName string = apim.name
output apimGatewayUrl string = apim.properties.gatewayUrl
output chatApiUrl string = chatApiUrl
output keycloakUrl string = kcUrl
output issuer string = issuer
output jwksUri string = jwksUri
output mcpServerUrl string = '${mcpUrl}/mcp'
output webUrl string = webUrl
