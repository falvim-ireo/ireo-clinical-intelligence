$ErrorActionPreference = 'Stop'
$BrowserMode = if ($env:IREO_AUTO_RUN_BROWSER_MODE) {
    $env:IREO_AUTO_RUN_BROWSER_MODE.Trim().ToLowerInvariant()
} else {
    'review'
}
if ($BrowserMode -notin @('review', 'headless', 'visible')) {
    throw 'IREO_AUTO_RUN_BROWSER_MODE deve ser review, headless ou visible.'
}
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$LockPath = Join-Path $ProjectRoot 'data\radiology_auto_run.lock'
$LockDirectory = Split-Path -Parent $LockPath
New-Item -ItemType Directory -Force -Path $LockDirectory | Out-Null

if (Test-Path -LiteralPath $LockPath) {
    $age = (Get-Date) - (Get-Item -LiteralPath $LockPath).LastWriteTime
    if ($age.TotalHours -lt 6) { exit 0 }
    Remove-Item -LiteralPath $LockPath -Force
}

New-Item -ItemType File -Path $LockPath -ErrorAction Stop | Out-Null
try {
    # O aplicativo gera somente dados sanitizados em data/last_auto_run_summary.json.
    & ireo-clinical-intelligence radiology-auto-run
    $ExitCode = $LASTEXITCODE
    $SummaryPath = Join-Path $ProjectRoot 'data\last_auto_run_summary.json'
    Write-Output "Exit code: $ExitCode"
    Write-Output "Resumo: $SummaryPath"
    if (Test-Path -LiteralPath $SummaryPath) {
        $Summary = Get-Content -LiteralPath $SummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([int]$Summary.review_required -gt 0) {
            Write-Output 'Pendências: consulte intake-review-list.'
            $ReportPath = Join-Path $ProjectRoot 'data\radiology_review_required.txt'
            if (Test-Path -LiteralPath $ReportPath) {
                Get-Content -LiteralPath $ReportPath -Encoding UTF8
            }
        }
        elseif ([int]$Summary.failed -gt 0) {
            Write-Output 'Falhas: consulte radiology-auto-status.'
        }
    }
}
finally {
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
}

exit $ExitCode
