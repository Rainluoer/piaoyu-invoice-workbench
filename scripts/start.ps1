$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskVenvPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
if (Test-Path -LiteralPath $taskVenvPython) {
    & $taskVenvPython (Join-Path $taskRoot 'app\launcher.py') @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 (Join-Path $taskRoot 'app\launcher.py') @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python (Join-Path $taskRoot 'app\launcher.py') @args
} else {
    throw '未找到 Python。请安装 Python 3.10 或以上，再运行安装依赖.cmd。'
}
exit $LASTEXITCODE
