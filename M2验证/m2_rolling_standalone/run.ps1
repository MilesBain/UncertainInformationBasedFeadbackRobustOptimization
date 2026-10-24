# 可选的PowerShell入口。也可直接 python -X utf8 main.py。
# 指定已装依赖的解释器：$env:M2_PYTHON = '你的环境\Scripts\python.exe'
$ErrorActionPreference = 'Stop'
$localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($env:M2_PYTHON) {
    $pythonExecutable = $env:M2_PYTHON
} elseif (Test-Path -LiteralPath $localPython) {
    $pythonExecutable = $localPython
} else {
    $pythonExecutable = (Get-Command python -ErrorAction Stop).Source
}
Push-Location $PSScriptRoot
try {
    & $pythonExecutable -X utf8 main.py @args
    $runExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $runExitCode
