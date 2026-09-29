// API Management configuration of the "chat" API, which plays the role of the BFF:
// validates the app token (myID / Keycloak), performs the On-Behalf-Of token exchange,
// delegates the end-user identity to the Foundry hosted agent. Policies live in ../apim/.
targetScope = 'resourceGroup'

param apimName string
param appInsightsName string
param keyVaultUri string
param kvIdentityClientId string
param chatIdentityClientId string
param oidcIssuer string
param appClientId string
param bffClientId string
param mcpAudience string
param mcpScopes string
param mcpRequiredScope string = 'weather:read'
param webOrigin string
param agentEndpoint string

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// ---------------------------------------------------------------- named values
var plainValues = {
  'oidc-issuer': oidcIssuer
  'oidc-config-url': '${oidcIssuer}/.well-known/openid-configuration'
  'oidc-token-endpoint': '${oidcIssuer}/protocol/openid-connect/token'
  'app-client-id': appClientId
  'app-audience': bffClientId
  'bff-client-id': bffClientId
  'mcp-audience': mcpAudience
  'mcp-scopes': mcpScopes
  'mcp-required-scope': mcpRequiredScope
  'web-origin': webOrigin
  'chat-mi-client-id': chatIdentityClientId
}

resource namedValues 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = [for item in items(plainValues): {
  parent: apim
  name: item.key
  properties: { displayName: item.key, value: item.value, secret: false }
}]

// Secret of the confidential client weather-bff: never copied, APIM reads it from Key Vault.
resource bffClientSecret 'Microsoft.ApiManagement/service/namedValues@2024-05-01' = {
  parent: apim
  name: 'bff-client-secret'
  properties: {
    displayName: 'bff-client-secret'
    secret: true
    keyVault: {
      secretIdentifier: '${keyVaultUri}secrets/weather-bff-client-secret'
      identityClientId: kvIdentityClientId
    }
  }
}

// ---------------------------------------------------------------- policy fragments
var fragments = {
  'reject-sensitive-headers': loadTextContent('../apim/fragments/reject-sensitive-headers.xml')
  'validate-app-token': loadTextContent('../apim/fragments/validate-app-token.xml')
  'obo-exchange': loadTextContent('../apim/fragments/obo-exchange.xml')
  'foundry-delegation': loadTextContent('../apim/fragments/foundry-delegation.xml')
}

resource policyFragments 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [for item in items(fragments): {
  parent: apim
  name: item.key
  dependsOn: [ namedValues, bffClientSecret ]
  properties: { format: 'rawxml', value: item.value }
}]

// ---------------------------------------------------------------- chat API
resource chatApi 'Microsoft.ApiManagement/service/apis@2024-05-01' = {
  parent: apim
  name: 'chat'
  properties: {
    displayName: 'Weather chat (BFF)'
    description: 'BFF of the weather chat: myID token validation, OBO token exchange, delegation to the Foundry hosted agent.'
    path: 'chat'
    protocols: [ 'https' ]
    subscriptionRequired: false
    serviceUrl: agentEndpoint
    apiType: 'http'
  }
}

resource chatApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = {
  parent: chatApi
  name: 'policy'
  dependsOn: [ policyFragments ]
  properties: { format: 'rawxml', value: loadTextContent('../apim/api-chat.xml') }
}

resource responsesOp 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: chatApi
  name: 'responses'
  properties: { displayName: 'Send a chat message', method: 'POST', urlTemplate: '/responses' }
}

resource responsesPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: responsesOp
  name: 'policy'
  dependsOn: [ policyFragments, chatApiPolicy ]
  properties: { format: 'rawxml', value: loadTextContent('../apim/op-responses.xml') }
}

resource meOp 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: chatApi
  name: 'me'
  properties: { displayName: 'Validated identity of the caller', method: 'GET', urlTemplate: '/me' }
}

resource mePolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = {
  parent: meOp
  name: 'policy'
  dependsOn: [ policyFragments, chatApiPolicy ]
  properties: { format: 'rawxml', value: loadTextContent('../apim/op-me.xml') }
}

// CORS preflight requests are answered by the cors policy of the API.
resource preflightResponses 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: chatApi
  name: 'preflight-responses'
  properties: { displayName: 'CORS preflight (responses)', method: 'OPTIONS', urlTemplate: '/responses' }
}

resource preflightMe 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = {
  parent: chatApi
  name: 'preflight-me'
  properties: { displayName: 'CORS preflight (me)', method: 'OPTIONS', urlTemplate: '/me' }
}

// ---------------------------------------------------------------- telemetry (no tokens, no bodies)
resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'appinsights'
  properties: {
    loggerType: 'applicationInsights'
    resourceId: appInsights.id
    credentials: { connectionString: appInsights.properties.ConnectionString }
  }
}

var noPayload = {
  request: { headers: [], body: { bytes: 0 } }
  response: { headers: [], body: { bytes: 0 } }
}

resource chatDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = {
  parent: chatApi
  name: 'applicationinsights'
  properties: {
    loggerId: logger.id
    alwaysLog: 'allErrors'
    sampling: { samplingType: 'fixed', percentage: 100 }
    verbosity: 'information'
    logClientIp: false
    httpCorrelationProtocol: 'W3C'
    frontend: noPayload
    backend: noPayload
  }
}
