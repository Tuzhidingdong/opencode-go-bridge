<#
.SYNOPSIS
    Antigravity + OpenCode Go Bridge: PowerShell One-Click Setup
#>

$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Antigravity + OpenCode Go Bridge: Setup Launcher" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonCmd) {
    Write-Host "[-] Python not found in PATH! Please install Python 3.10+." -ForegroundColor Red
    exit 1
}

Write-Host "[+] Found Python: $pythonCmd" -ForegroundColor Green
$scriptPath = Join-Path $PSScriptRoot "install.py"

& python $scriptPath
