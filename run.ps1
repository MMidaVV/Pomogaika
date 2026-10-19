$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m uvicorn pomogaika.web.app:app --host 127.0.0.1 --port 8000
