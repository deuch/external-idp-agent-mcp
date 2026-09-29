<#
.SYNOPSIS
  Removes an environment deployed by deploy.ps1.
  - resource group created by deploy.ps1: the whole group is deleted;
  - pre-existing resource group (AZURE_USE_EXISTING_RESOURCE_GROUP=true): all its resources are
    deleted, the group itself and its tags are kept.

  Before deleting: exports the Keycloak realm to backups/<env>/ (secrets masked by Keycloak) and,
  unless -ExportSecrets:$false, the Key Vault secrets to a file OUTSIDE the repository.

.EXAMPLE
  ./teardown.ps1 -Environment weather-kc -WhatIf    # show what would be deleted
  ./teardown.ps1 -Environment weather-kc -Purge      # delete + purge soft-deleted Foundry account / Key Vault / API Management
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory)][string]$Environment,
    [bool]$ExportSecrets = $true,
    [string]$SecretsExportPath = (Join-Path $env:USERPROFILE "weather-poc-$Environment-secrets-$(Get-Date -Format yyyyMMddHHmmss).json"),
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$env:AZURE_DEV_USER_AGENT = 'microsoft_foundry_skill'
$root = $PSScriptRoot
$agentDir = Join-Path $root 'weather-agent'
$python = Join-Path $root '.venv-dev/Scripts/python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

Push-Location $agentDir
try {
    $values = @{}
    foreach ($line in (azd env get-values -e $Environment)) { if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)="?(.*?)"?$') { $values[$Matches[1]] = $Matches[2] } }
} finally { Pop-Location }
$rg = $values.AZURE_RESOURCE_GROUP
if (-not $rg) { throw "No AZURE_RESOURCE_GROUP in azd environment '$Environment'" }
if ((az group exists -n $rg) -ne 'true') { Write-Host "Resource group $rg does not exist."; return }

$resources = az resource list -g $rg -o json | ConvertFrom-Json
$vault = ($resources | Where-Object type -eq 'Microsoft.KeyVault/vaults' | Select-Object -First 1).name
$accounts = ($resources | Where-Object type -eq 'Microsoft.CognitiveServices/accounts').name
$apims = ($resources | Where-Object type -eq 'Microsoft.ApiManagement/service').name
Write-Host "Environment $Environment -> resource group $rg ($($resources.Count) resources)" -ForegroundColor Cyan
$resources | Sort-Object type | ForEach-Object { Write-Host ("  {0,-55} {1}" -f $_.type, $_.name) }

$backupDir = Join-Path $root "backups/$Environment"
New-Item -ItemType Directory -Force $backupDir | Out-Null
if ($vault) {
    $kcFqdn = az containerapp show -g $rg -n ca-keycloak --query "properties.configuration.ingress.fqdn" -o tsv 2>$null
    if ($kcFqdn) {
        $env:KC_URL = "https://$kcFqdn"
        $env:KC_ADMIN_PASSWORD = az keyvault secret show --vault-name $vault --name kc-admin-password --query value -o tsv
        try { & $python "$root/platform/keycloak/configure_realm.py" export (Join-Path $backupDir 'realm-export-before-teardown.json') }
        catch { Write-Warning "realm export failed: $_" }
        finally { Remove-Item Env:KC_ADMIN_PASSWORD -ErrorAction SilentlyContinue }
    }
    if ($ExportSecrets -and -not $WhatIfPreference) {
        $dump = [ordered]@{}
        foreach ($n in (az keyvault secret list --vault-name $vault --query "[].name" -o tsv)) {
            $dump[$n] = az keyvault secret show --vault-name $vault --name $n --query value -o tsv
        }
        $dump | ConvertTo-Json | Set-Content -Path $SecretsExportPath -Encoding utf8
        Write-Host "Secrets exported to $SecretsExportPath (outside the repository - protect or delete this file)" -ForegroundColor Yellow
    }
}

if ($PSCmdlet.ShouldProcess($rg, $(if ($values.AZURE_USE_EXISTING_RESOURCE_GROUP -eq 'true') { 'Delete all resources (keep the pre-existing resource group and its tags)' } else { 'Delete resource group' }))) {
    if ($values.AZURE_USE_EXISTING_RESOURCE_GROUP -eq 'true') {
        # The resource group was provided by its owner: empty it, keep it (and its tags).
        $empty = Join-Path $env:TEMP 'empty-template.json'
        '{"$schema":"https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#","contentVersion":"1.0.0.0","resources":[]}' | Set-Content $empty
        az deployment group create -g $rg -n teardown-empty --mode Complete -f $empty -o none
        Remove-Item $empty
    } else {
        az group delete -n $rg --yes
    }
    $role = az role definition list --custom-role-only true -o json | ConvertFrom-Json |
        Where-Object { $_.roleName -eq "Foundry Agent User Identity Impersonation ($rg)" }
    if ($role) { az role definition delete --name $role.name -o none }
    if ($Purge) {
        foreach ($a in $accounts) { az cognitiveservices account purge -g $rg -n $a -l $values.AZURE_LOCATION 2>$null }
        if ($vault) { az keyvault purge -n $vault 2>$null }
        foreach ($s in $apims) { az apim deletedservice purge --service-name $s --location $values.AZURE_LOCATION 2>$null }
    }
    Write-Host "Environment $Environment removed. Local azd environment kept: weather-agent/.azure/$Environment" -ForegroundColor Green
}
