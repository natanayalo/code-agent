$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot '../../scripts/m2_preservation.ps1'
$receipt = Join-Path $env:TEMP ("m2-preservation-test-{0}.json" -f [guid]::NewGuid())
function global:docker { $global:LASTEXITCODE = 1 }
try {
    foreach ($compare in @($false, $true)) {
        $output = ''
        $failed = $false
        try { $output = & $script -Receipt $receipt -Compare:$compare | Out-String }
        catch { $failed = $_.Exception.Message -match 'Docker container enumeration failed' }
        if (-not $failed) { throw "Expected enumeration failure (Compare=$compare); output: $output" }
        if (Test-Path -LiteralPath $receipt) { throw 'Unexpected baseline receipt' }
        if ($output -match 'PASS|Saved sanitized') { throw 'Unexpected success output' }
    }
    Write-Output 'PASS: enumeration failure prevents save and compare success'
} finally {
    Remove-Item Function:global:docker -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $receipt) { Remove-Item -LiteralPath $receipt }
}
