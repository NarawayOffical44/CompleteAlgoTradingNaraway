$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:TRADING_MODE = "paper"
$env:STARTING_CAPITAL = "1000"
.\.venv\Scripts\python.exe main.py --headless
