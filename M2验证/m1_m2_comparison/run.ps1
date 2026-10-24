param(
    [string]$Python = '',
    [string]$Output = '',
    [switch]$SolverLog,
    [switch]$NoFigures
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
$arguments = @((Join-Path $PSScriptRoot 'run_comparison.py'), '--output', $Output)
if ($SolverLog) { $arguments += '--solver-log' }
if ($NoFigures) { $arguments += '--no-figures' }
& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "Comparison exited with code $LASTEXITCODE" }
