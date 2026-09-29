<#
.SYNOPSIS
  Deploys the whole POC into one resource group (one azd environment):
    1. azd environment (created if missing)            5. PostgreSQL + Keycloak + MCP + web app + APIM config
    2. Foundry project + model (azd provision)         6. Keycloak realm (platform/keycloak/realm-config.json)
    3. Registry, Key Vault, identities, APIM (base)     7. Hosted agent (azd deploy)
    4. Secrets (Key Vault) + images (ACR remote builds) 8. End-to-end test
  No local Docker required. Idempotent: re-running reuses the secrets stored in Key Vault.

.EXAMPLE
  ./deploy.ps1                           # settings from platform/.env
  ./deploy.ps1 -SkipImages -SkipTests    # configuration-only update
#>
[CmdletBinding()]
param(
    [string]$ConfigFile = "$PSScriptRoot/platform/.env",
    [switch]$SkipImages,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
$root = $PSScriptRoot
$agentDir = Join-Path $root 'weather-agent'
$template = Join-Path $root 'platform/infra/main.bicep'
$python = Join-Path $root '.venv-dev/Scripts/python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

function Read-DotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path $Path)) { throw "Config file not found: $Path (copy platform/.env.example)" }
    foreach ($line in Get-Content $Path) {
        if ($line -notmatch '^\s*#' -and $line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

function Invoke-Az {
    $out = & az @args
    # Never echo the arguments: some of them are secrets (e.g. the database password).
    if ($LASTEXITCODE) { throw "az $($args[0]) $($args[1]) failed (exit code $LASTEXITCODE)" }
    return $out
}

function Invoke-Azd {
    Push-Location $agentDir
    try { & azd @args; if ($LASTEXITCODE) { throw "azd $($args -join ' ') failed" } } finally { Pop-Location }
}

function Get-AzdValues([string]$Name) {
    Push-Location $agentDir
    try {
        $values = @{}
        foreach ($line in (azd env get-values -e $Name)) { if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)="?(.*?)"?$') { $values[$Matches[1]] = $Matches[2] } }
        return $values
    } finally { Pop-Location }
}

function New-Secret([int]$Length = 32) {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'.ToCharArray()
    $bytes = [byte[]]::new($Length); [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    # Prefix guarantees upper, lower and digit characters (PostgreSQL complexity rules).
    return 'Kc9' + (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] }))
}

function Get-OrCreateSecret([string]$Vault, [string]$Name) {
    $value = az keyvault secret show --vault-name $Vault --name $Name --query value -o tsv 2>$null
    if ($LASTEXITCODE -or -not $value) {
        $value = New-Secret
        Invoke-Az keyvault secret set --vault-name $Vault --name $Name --value $value --output none | Out-Null
        Write-Host "   secret '$Name' created"
    }
    return $value
}

# ---------------------------------------------------------------- settings
$cfg = Read-DotEnv $ConfigFile
$envName = if ($cfg.AZD_ENVIRONMENT) { $cfg.AZD_ENVIRONMENT } else { 'weather-kc' }
$subscription = if ($cfg.AZURE_SUBSCRIPTION_ID) { $cfg.AZURE_SUBSCRIPTION_ID } else { Invoke-Az account show --query id -o tsv }
$location = if ($cfg.AZURE_LOCATION) { $cfg.AZURE_LOCATION } else { 'francecentral' }
$rg = if ($cfg.AZURE_RESOURCE_GROUP) { $cfg.AZURE_RESOURCE_GROUP } else { "rg-$envName" }
$projectName = if ($cfg.AZURE_AI_PROJECT_NAME) { $cfg.AZURE_AI_PROJECT_NAME } else { "ai-project-$envName" }
$kcVersion = if ($cfg.KEYCLOAK_VERSION) { $cfg.KEYCLOAK_VERSION } else { '26.7.4' }
$backupDir = Join-Path $root "backups/$envName"
New-Item -ItemType Directory -Force $backupDir | Out-Null

Write-Host "==> 1/8 azd environment '$envName' (resource group $rg)" -ForegroundColor Cyan
# An existing resource group is reused as is: its location wins and its tags are never modified
# (weather-agent/infra/main.bicep only creates the group when it does not exist).
$useExistingRg = (Invoke-Az group exists -n $rg --subscription $subscription) -eq 'true'
$rgTagsBefore = @{}
if ($useExistingRg) {
    $rgInfo = Invoke-Az group show -n $rg --subscription $subscription -o json | ConvertFrom-Json
    $location = $rgInfo.location
    if ($rgInfo.tags) { $rgInfo.tags.PSObject.Properties | ForEach-Object { $rgTagsBefore[$_.Name] = $_.Value } }
    Write-Host "   using existing resource group $rg ($location), tags preserved: $(($rgTagsBefore.Keys | Sort-Object) -join ', ')"
}

function Assert-RgTagsUnchanged([string]$Step) {
    if (-not $useExistingRg) { return }
    $now = @{}
    $tags = (Invoke-Az group show -n $rg --subscription $subscription -o json | ConvertFrom-Json).tags
    if ($tags) { $tags.PSObject.Properties | ForEach-Object { $now[$_.Name] = $_.Value } }
    foreach ($k in $rgTagsBefore.Keys) {
        if (-not $now.ContainsKey($k) -or $now[$k] -ne $rgTagsBefore[$k]) { throw "Resource group tag '$k' was modified during step '$Step'" }
    }
}

Push-Location $agentDir
$existing = (azd env list --output json | ConvertFrom-Json).Name
Pop-Location
if ($existing -notcontains $envName) {
    Invoke-Azd env new $envName --subscription $subscription --location $location --no-prompt | Out-Null
}
$settings = [ordered]@{
    AZURE_SUBSCRIPTION_ID = $subscription; AZURE_LOCATION = $location; AZURE_RESOURCE_GROUP = $rg
    AZURE_USE_EXISTING_RESOURCE_GROUP = $useExistingRg.ToString().ToLower()
    AZURE_AI_PROJECT_NAME = $projectName; AZURE_AI_MODEL_DEPLOYMENT_NAME = 'gpt-5.4-mini'
    USE_EXISTING_AI_PROJECT = 'false'; AZD_AGENT_SKIP_ACR = 'true'; MCP_AUDIENCE = 'weather-mcp'
}
foreach ($k in $settings.Keys) { Invoke-Azd env set $k $settings[$k] -e $envName | Out-Null }

Write-Host "==> 2/8 Foundry project + model (azd provision, weather-agent/infra)" -ForegroundColor Cyan
Invoke-Azd provision -e $envName --no-prompt
Assert-RgTagsUnchanged 'azd provision'
$azd = Get-AzdValues $envName
$deployer = Invoke-Az ad signed-in-user show --query id -o tsv
$common = @(
    "deployerObjectId=$deployer", "foundryAccountName=$($azd.AZURE_AI_ACCOUNT_NAME)",
    "foundryProjectName=$($azd.AZURE_AI_PROJECT_NAME)", "foundryProjectEndpoint=$($azd.FOUNDRY_PROJECT_ENDPOINT)"
)

Write-Host "==> 3/8 Registry, Container Apps environment, Key Vault, identities, API Management" -ForegroundColor Cyan
$base = Invoke-Az deployment group create -g $rg -n platform-base -f $template --parameters stage=base @common --query properties.outputs -o json | ConvertFrom-Json
$vault = $base.keyVaultName.value
$registry = $base.registryName.value
Start-Sleep -Seconds 20  # RBAC propagation for the deployer on the vault

Write-Host "==> 4/8 Secrets (generated once, stored only in Key Vault $vault) and images" -ForegroundColor Cyan
$secrets = @{}
foreach ($name in 'kc-admin-password', 'kc-db-password', 'weather-bff-client-secret', 'alice-password', 'bob-password') {
    $secrets[$name] = Get-OrCreateSecret $vault $name
}
$tag = Get-Date -Format 'yyyyMMddHHmmss'
if (-not $SkipImages) {
    Invoke-Az acr build -r $registry -t "keycloak:$tag" --build-arg "KC_VERSION=$kcVersion" "$root/platform/keycloak" --no-logs -o none | Out-Null
    Invoke-Az acr build -r $registry -t "mcp-weather:$tag" "$root/platform/src/mcp-weather" --no-logs -o none | Out-Null
    Invoke-Az acr build -r $registry -t "web:$tag" "$root/platform/src/web" --no-logs -o none | Out-Null
    $images = @{ kc = "$registry.azurecr.io/keycloak:$tag"; mcp = "$registry.azurecr.io/mcp-weather:$tag"; web = "$registry.azurecr.io/web:$tag" }
} else {
    $images = @{}
    foreach ($pair in @(@('kc', 'ca-keycloak'), @('mcp', 'ca-mcp-weather'), @('web', 'ca-web'))) {
        $images[$pair[0]] = Invoke-Az containerapp show -g $rg -n $pair[1] --query "properties.template.containers[0].image" -o tsv
    }
}

Write-Host "==> 5/8 PostgreSQL + Keycloak + MCP server + web app" -ForegroundColor Cyan
$appParams = @(
    "keycloakImage=$($images.kc)", "mcpImage=$($images.mcp)", "webImage=$($images.web)", "dbAdminPassword=$($secrets['kc-db-password'])"
)
$out = Invoke-Az deployment group create -g $rg -n platform-apps -f $template --parameters stage=apps @common @appParams `
    --query properties.outputs -o json | ConvertFrom-Json
Assert-RgTagsUnchanged 'platform deployment'
$kcUrl = $out.keycloakUrl.value; $issuer = $out.issuer.value; $webUrl = $out.webUrl.value; $chatApiUrl = $out.chatApiUrl.value; $mcpUrl = $out.mcpServerUrl.value
Write-Host "   waiting for Keycloak ($kcUrl)..."
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-RestMethod "$kcUrl/realms/master/.well-known/openid-configuration" -TimeoutSec 10 | Out-Null; break } catch { Start-Sleep 10 }
    if ($i -eq 59) { throw 'Keycloak did not become ready' }
}

Write-Host "==> 6/8 Keycloak realm (realm-config.json, idempotent)" -ForegroundColor Cyan
$env:KC_URL = $kcUrl; $env:KC_ADMIN_PASSWORD = $secrets['kc-admin-password']; $env:WEB_URL = $webUrl
$env:BFF_CLIENT_SECRET = $secrets['weather-bff-client-secret']; $env:ALICE_PASSWORD = $secrets['alice-password']; $env:BOB_PASSWORD = $secrets['bob-password']
try {
    & $python "$root/platform/keycloak/configure_realm.py" apply
    if ($LASTEXITCODE) { throw 'realm configuration failed' }
    & $python "$root/platform/keycloak/configure_realm.py" export (Join-Path $backupDir "realm-export-$tag.json")
} finally {
    Remove-Item Env:BFF_CLIENT_SECRET -ErrorAction SilentlyContinue
}

Write-Host "==> 6b/8 API Management chat API = BFF (policies need the realm OpenID configuration)" -ForegroundColor Cyan
Invoke-Az deployment group create -g $rg -n platform-gateway -f $template --parameters stage=gateway @common @appParams -o none | Out-Null
Assert-RgTagsUnchanged 'gateway deployment'

Write-Host "==> 7/8 Hosted agent (azd deploy)" -ForegroundColor Cyan
Invoke-Azd env set MCP_SERVER_URL $mcpUrl -e $envName | Out-Null
Invoke-Azd env set OIDC_ISSUER $issuer -e $envName | Out-Null
Invoke-Azd env set OIDC_JWKS_URI $out.jwksUri.value -e $envName | Out-Null
Invoke-Azd deploy weather-agent -e $envName --no-prompt
Assert-RgTagsUnchanged 'azd deploy'

# Non-secret deployment record (useful to compare or rebuild an environment).
[ordered]@{
    deployedAt = (Get-Date).ToString('o'); azdEnvironment = $envName; subscription = $subscription; resourceGroup = $rg
    existingResourceGroup = $useExistingRg; resourceGroupTags = $rgTagsBefore
    foundryProjectEndpoint = $azd.FOUNDRY_PROJECT_ENDPOINT; agent = 'weather-agent'; keyVault = $vault; registry = $registry
    images = $images; keycloakUrl = $kcUrl; issuer = $issuer; mcpServerUrl = $mcpUrl; webUrl = $webUrl; chatApiUrl = $chatApiUrl; apim = $out.apimName.value
} | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 (Join-Path $backupDir 'deployment.json')

$testExit = 0
if (-not $SkipTests) {
    Write-Host "==> 8/8 End-to-end test (password grant enabled temporarily on weather-mobile)" -ForegroundColor Cyan
    & $python "$root/platform/keycloak/configure_realm.py" direct-grant on | Out-Null
    try {
        $env:KC_ISSUER = $issuer; $env:API_URL = $chatApiUrl; $env:WEB_URL = $webUrl; $env:MCP_URL = $mcpUrl
        & $python "$root/tools/e2e_test.py"
        $testExit = $LASTEXITCODE
    } finally {
        & $python "$root/platform/keycloak/configure_realm.py" direct-grant off
    }
}
Remove-Item Env:KC_ADMIN_PASSWORD, Env:ALICE_PASSWORD, Env:BOB_PASSWORD -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Deployment complete ($envName / $rg)" -ForegroundColor Green
Write-Host "  Chat (web app)    : $webUrl"
Write-Host "  BFF (APIM API)   : $chatApiUrl"
Write-Host "  Keycloak (myID)  : $kcUrl/admin   (user 'admin', password: Key Vault $vault / kc-admin-password)"
Write-Host "  Issuer           : $issuer"
Write-Host "  MCP server       : $mcpUrl"
Write-Host "  Test users       : alice (premium) / bob (basic) - passwords in Key Vault: alice-password, bob-password"
if ($testExit) { throw "End-to-end test reported failures" }
