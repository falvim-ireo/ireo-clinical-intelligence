$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
& ireo-clinical-intelligence intake-review-list
Write-Output 'Para retomar: ireo-clinical-intelligence intake-review-resume --correlation-id <id>'
exit $LASTEXITCODE
