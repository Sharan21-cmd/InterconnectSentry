
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Starting InterconnectSentry..." -ForegroundColor Cyan

if (!(Test-Path ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

$env:INTERCONNECTSENTRY_FPGA_BRIDGE_URL = "http://127.0.0.1:8765"

Start-Process powershell.exe -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$PSScriptRoot'; py hardware\bridge.py"
)

Start-Sleep -Seconds 2

Start-Process powershell.exe -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$PSScriptRoot'; `$env:INTERCONNECTSENTRY_FPGA_BRIDGE_URL='http://127.0.0.1:8765'; & .\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000"
)

Start-Process powershell.exe -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$PSScriptRoot'; py -m http.server 5173 --directory frontend"
)

Start-Sleep -Seconds 4
Start-Process "http://127.0.0.1:5173"

Write-Host "InterconnectSentry startup commands launched." -ForegroundColor Green
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "Backend:  http://127.0.0.1:8000/docs"
Write-Host "FPGA:     http://127.0.0.1:8765/api/status"
