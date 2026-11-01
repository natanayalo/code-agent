param(
    [string]$Receipt = (Join-Path $PSScriptRoot '../artifacts/m2-preservation-canonical.json'),
    [switch]$Compare
)
# Read-only Docker/Tailscale metadata; secrets are hashed in memory, never emitted.
$ErrorActionPreference = 'Stop'
function Get-M2Baseline {
    $items = @()
    foreach ($containerId in (docker ps -aq)) {
        $item = (docker inspect $containerId | ConvertFrom-Json)[0]
        if ($LASTEXITCODE -ne 0) { throw 'Container metadata inspection failed' }
        if ($item.Config.Labels.'code-agent.m2.owner' -eq 'security-validation') { continue }
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        $configuration = [ordered]@{
            config = $item.Config
            host = $item.HostConfig
            mounts = $item.Mounts
        } | ConvertTo-Json -Depth 40 -Compress
        $digest = [Convert]::ToHexString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($configuration)))
        $items += [pscustomobject]@{
            name = $item.Name
            id = $item.Id
            image = $item.Image
            state = $item.State.Status
            configuration_sha256 = $digest
            networks = @($item.NetworkSettings.Networks.PSObject.Properties.Name | Sort-Object)
            ports = $item.HostConfig.PortBindings
        }
    }
    $serve = & 'C:/Program Files/Tailscale/tailscale.exe' serve status --json
    if ($LASTEXITCODE -ne 0) { throw 'Tailscale metadata inspection failed' }
    return [pscustomobject]@{
        captured_utc = [DateTime]::UtcNow.ToString('o')
        containers = $items
        serve = (($serve | ConvertFrom-Json) | ConvertTo-Json -Depth 20 -Compress)
    }
}
$current = Get-M2Baseline
if ($Compare) {
    $baseline = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
    foreach ($expected in $baseline.containers) {
        $actual = @($current.containers | Where-Object id -eq $expected.id)
        if ($actual.Count -ne 1) { throw "Original container missing: $($expected.name)" }
        foreach ($field in @('image', 'state', 'configuration_sha256')) {
            if ($actual[0].$field -ne $expected.$field) { throw "Changed $field for $($expected.name)" }
        }
        if (($actual[0].networks -join ',') -ne ($expected.networks -join ',')) {
            throw "Changed networks for $($expected.name)"
        }
    }
    if ($current.serve -ne $baseline.serve) { throw 'Tailscale Serve changed' }
    Write-Output "PASS: $($baseline.containers.Count) original containers and Serve preserved."
} else {
    if (Test-Path -LiteralPath $Receipt) { throw 'Refusing to overwrite an existing baseline receipt' }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Receipt) | Out-Null
    $current | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Receipt -Encoding utf8
    Write-Output "Saved sanitized metadata for $($current.containers.Count) original containers."
}
