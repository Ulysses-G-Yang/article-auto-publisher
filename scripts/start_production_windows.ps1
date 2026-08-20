#Requires -Version 5.1

<##
.SYNOPSIS
    启动当前项目的生产 Flask（Waitress）和 MCP 服务。

.DESCRIPTION
    默认以隐藏窗口启动服务，日志写入 data\logs；Chrome 仍会按需以有头模式打开。
    只匹配当前项目目录下的进程，不会停止或覆盖其他项目，也不会结束 Chrome。
#>
[CmdletBinding()]
param(
    [switch]$Visible
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
$envFile = Join-Path $ProjectRoot "data\production_env.ps1"
if (-not (Test-Path -LiteralPath $envFile)) {
    throw "找不到生产配置 $envFile。请先运行 .\scripts\setup_windows.ps1。"
}
. $envFile

if ($env:APP_ENV -ne "production") {
    throw "APP_ENV 不是 production，拒绝启动生产入口。"
}

# MCP 入口以模块方式启动；发布包不安装本地源码为 site-package，显式加入
# 随包的 src 目录，确保 account_sessions/article_mvp 等运行模块可解析。
$srcRoot = Join-Path $ProjectRoot "src"
$env:PYTHONPATH = if ($env:PYTHONPATH) {
    "$srcRoot;$($env:PYTHONPATH)"
} else {
    $srcRoot
}

$condaCommand = Get-Command conda -ErrorAction SilentlyContinue
if (-not $condaCommand) {
    throw "找不到 conda。请先运行 setup_windows.ps1。"
}
$condaExecutable = if ($condaCommand.Source -and (Test-Path $condaCommand.Source)) { $condaCommand.Source } else { $condaCommand.Name }
$condaBase = ((& $condaExecutable info --base) | Select-Object -Last 1).ToString().Trim()
$pythonPath = Join-Path $condaBase "envs\article-publisher-py312\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到 article-publisher-py312 Python。请先运行 setup_windows.ps1。"
}
$projectRootMarker = $ProjectRoot.ToLowerInvariant()

$flaskPort = if ($env:FLASK_PORT) { [int]$env:FLASK_PORT } else { 5000 }
$mcpPort = if ($env:MCP_PORT) { [int]$env:MCP_PORT } else { 8765 }

$projectProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -and
        $_.CommandLine.ToLowerInvariant().Contains($projectRootMarker) -and
        ($_.CommandLine -match "run_flask_production\.py|mcp_server\.server")
    }
if ($projectProcesses) {
    $details = $projectProcesses | ForEach-Object { "PID=$($_.ProcessId) $($_.CommandLine)" }
    throw "本项目生产服务已经运行：$($details -join ' | ')"
}

function Assert-PortFree {
    param([int]$Port)
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listeners) {
        $owners = $listeners | ForEach-Object { "PID=$($_.OwningProcess) $($_.LocalAddress):$($_.LocalPort)" }
        throw "端口 $Port 已被占用：$($owners -join ', ')"
    }
}

Assert-PortFree $flaskPort
Assert-PortFree $mcpPort

$logsDir = Join-Path $ProjectRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$flaskOut = Join-Path $logsDir "production-flask.stdout.log"
$flaskErr = Join-Path $logsDir "production-flask.stderr.log"
$mcpOut = Join-Path $logsDir "production-mcp.stdout.log"
$mcpErr = Join-Path $logsDir "production-mcp.stderr.log"
$windowStyle = if ($Visible) { "Normal" } else { "Hidden" }
$flaskProcess = $null
$mcpProcess = $null

try {
    $flaskProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @((Join-Path $ProjectRoot "run_flask_production.py")) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle $windowStyle `
        -RedirectStandardOutput $flaskOut `
        -RedirectStandardError $flaskErr `
        -PassThru

    $mcpProcess = Start-Process -FilePath $pythonPath `
        -ArgumentList @("-m", "mcp_server.server", "--article-publisher-root=$ProjectRoot") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle $windowStyle `
        -RedirectStandardOutput $mcpOut `
        -RedirectStandardError $mcpErr `
        -PassThru

    function Wait-HttpOk {
        param(
            [Parameter(Mandatory = $true)][string]$Url,
            [hashtable]$Headers = @{},
            [int]$Seconds = 30
        )
        for ($attempt = 0; $attempt -lt $Seconds; $attempt++) {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Headers $Headers -Uri $Url -TimeoutSec 3
                if ($response.StatusCode -eq 200) {
                    return
                }
            } catch {
                # 服务启动过程中短暂不可用，继续等待。
            }
            Start-Sleep -Seconds 1
        }
        throw "健康检查超时: $Url"
    }

    Wait-HttpOk -Url "http://127.0.0.1:${flaskPort}/api/status"

    $probeHost = $env:MCP_BIND_HOST
    if ($probeHost -eq "0.0.0.0" -or $probeHost -eq "::") {
        $probeHost = (($env:MCP_ALLOWED_HOSTS -split ",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch "^(localhost|127\.0\.0\.1)$" } | Select-Object -First 1)
    }
    if (-not $probeHost) {
        throw "无法确定 MCP 健康检查 Host。"
    }
    Wait-HttpOk -Url "http://${probeHost}:${mcpPort}/healthz" -Headers @{ Host = $probeHost }
} catch {
    if ($flaskProcess -and -not $flaskProcess.HasExited) { Stop-Process -Id $flaskProcess.Id -Force -ErrorAction SilentlyContinue }
    if ($mcpProcess -and -not $mcpProcess.HasExited) { Stop-Process -Id $mcpProcess.Id -Force -ErrorAction SilentlyContinue }
    throw
}

Write-Host "生产服务已启动。" -ForegroundColor Green
Write-Host "Flask PID=$($flaskProcess.Id): http://127.0.0.1:${flaskPort}" -ForegroundColor Cyan
Write-Host "MCP PID=$($mcpProcess.Id): http://${probeHost}:${mcpPort}/mcp" -ForegroundColor Cyan
Write-Host "日志目录: $logsDir" -ForegroundColor Cyan
