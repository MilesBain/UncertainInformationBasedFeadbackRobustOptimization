param(
    [string]$Python = '',
    [int]$Steps = 23,
    [string]$Output = '',
    [switch]$SolverLog
)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $localRuntime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $verifiedRuntime = 'C:\Users\29909\Documents\Codex\2026-10-22\du-q\work\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localRuntime) {
        $Python = $localRuntime
    } elseif (Test-Path -LiteralPath $verifiedRuntime) {
        $Python = $verifiedRuntime
    } else {
        $Python = (Get-Command python -ErrorAction Stop).Source
    }
}
if (-not $Output) { $Output = Join-Path $PSScriptRoot 'results' }
$arguments = @((Join-Path $PSScriptRoot 'run_m2.py'), '--steps', $Steps, '--output', $Output)
if ($SolverLog) { $arguments += '--solver-log' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "M2 exited with code $LASTEXITCODE" }
