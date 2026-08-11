#Requires -Version 5.1

<##
.SYNOPSIS
    停止当前项目的生产 Flask 和 MCP 服务，不结束 Chrome。
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectRootMarker = $ProjectRoot.ToLowerInvariant()

$targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -and
        $_.CommandLine.ToLowerInvariant().Contains($projectRootMarker) -and
        ($_.CommandLine -match "run_flask_production\.py|mcp_server\.server")
    }

if (-not $targets) {
    Write-Host "当前没有发现本项目生产服务。" -ForegroundColor Yellow
    exit 0
}

foreach ($target in $targets) {
    Write-Host "停止 PID=$($target.ProcessId)" -ForegroundColor Cyan
    Stop-Process -Id ([int]$target.ProcessId) -Force
}

Write-Host "本项目 Flask/MCP 已停止；Chrome 和 Profile 未处理。" -ForegroundColor Green
