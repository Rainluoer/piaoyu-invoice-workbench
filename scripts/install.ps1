$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    $env:PYTHONUTF8 = '1'
    $taskVenvPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskVenvPython)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv .venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv .venv
        } else {
            throw '请先安装 Python 3.10 或以上，并勾选 Add Python to PATH。'
        }
        if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败，请检查 Python 安装。' }
    }
    & $taskVenvPython -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
    if ($LASTEXITCODE -ne 0) { throw '需要 Python 3.10 或以上。' }
    & $taskVenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络后重试。' }
    Write-Host '安装完成，可以双击启动工作台.cmd。'
} finally {
    Pop-Location
}
