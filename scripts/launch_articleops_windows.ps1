#Requires -Version 5.1

<##
.SYNOPSIS
    从桌面快捷方式安全打开 ArticleOps。

.DESCRIPTION
    仅信任当前安装目录的生产进程。服务健康时直接打开管理页；服务停止或当前
    安装目录的进程失去响应时，加载已有生产配置并复用正式启停脚本恢复服务。
    不会结束其他目录或其他程序的进程。
#>
[CmdletBinding()]
param(
    [int]$MutexTimeoutSeconds = 15,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ExpectedFlaskEntry = (Join-Path $ProjectRoot "run_flask_production.py").ToLowerInvariant()
$ProjectRootMarker = $ProjectRoot.ToLowerInvariant()
$LogsDirectory = Join-Path $ProjectRoot "data\logs"
$LauncherLog = Join-Path $LogsDirectory "articleops-launcher.log"

function Write-LauncherLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    try {
        New-Item -ItemType Directory -Force -Path $LogsDirectory | Out-Null
        $timestamp = [DateTime]::UtcNow.ToString("o")
        Add-Content -LiteralPath $LauncherLog -Value "[$timestamp] $Message" -Encoding UTF8
    } catch {
        # 日志写入失败不能掩盖真正的启动结果。
    }
}

function Test-WebHealth {
    param([Parameter(Mandatory = $true)][string]$Url)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-McpHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$HostHeader
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers @{ Host = $HostHeader } -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-ListenerOwner {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        return $null
    }

    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ownerIds.Count -ne 1) {
        throw "端口 $Port 存在多个监听进程，拒绝自动处理。"
    }

    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($ownerIds[0])" -ErrorAction SilentlyContinue
    if (-not $owner) {
        throw "无法确认端口 $Port 的监听进程，拒绝自动处理。"
    }
    return $owner
}

function Test-CurrentInstallationProcess {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][ValidateSet("Flask", "Mcp")][string]$Kind
    )

    if (-not $Process.CommandLine) {
        return $false
    }
    $commandLine = $Process.CommandLine.ToLowerInvariant()
    if ($Process.Name -ne "python.exe" -or -not $commandLine.Contains($ProjectRootMarker)) {
        return $false
    }
    if ($Kind -eq "Flask") {
        return $commandLine.Contains($ExpectedFlaskEntry)
    }
    return $commandLine.Contains("mcp_server.server")
}

function Invoke-ProjectScript {
    param([Parameter(Mandatory = $true)][string]$Name)

    $scriptPath = Join-Path $ProjectRoot "scripts\$Name"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "当前安装目录缺少 scripts\$Name。"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "$Name 执行失败（exit=$LASTEXITCODE）。"
    }
}

function Show-LauncherFailure {
    param([Parameter(Mandatory = $true)][string]$Message)

    $display = "ArticleOps 无法启动。`r`n`r`n$Message`r`n`r`n日志：$LauncherLog"
    if ($NoBrowser) {
        Write-Host $display -ForegroundColor Red
        return
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show(
            $display,
            "ArticleOps 启动失败",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        )
    } catch {
        Write-Host $display -ForegroundColor Red
    }
}

$mutex = $null
$mutexAcquired = $false
try {
    $environmentFile = Join-Path $ProjectRoot "data\production_env.ps1"
    if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
        throw "缺少 data\production_env.ps1，请先由管理员完成一次初始化。"
    }
    try {
        . $environmentFile
    } catch {
        throw "生产配置无法读取，请联系管理员检查 data\production_env.ps1。"
    }
    if ($env:APP_ENV -ne "production") {
        throw "生产配置中的 APP_ENV 不是 production。"
    }

    $flaskPort = if ($env:FLASK_PORT) { [int]$env:FLASK_PORT } else { 5000 }
    $mcpPort = if ($env:MCP_PORT) { [int]$env:MCP_PORT } else { 8765 }
    $WebUrl = "http://127.0.0.1:${flaskPort}/upload"
    $healthUrl = "http://127.0.0.1:${flaskPort}/api/status"

    $allowedMcpHosts = @(
        ($env:MCP_ALLOWED_HOSTS -split ",") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    )
    $mcpHealthHost = @($allowedMcpHosts | Where-Object { $_ -in @("127.0.0.1", "localhost") } | Select-Object -First 1)
    if ($mcpHealthHost.Count -eq 0) {
        $mcpHealthHost = @($allowedMcpHosts | Select-Object -First 1)
    }
    if ($mcpHealthHost.Count -eq 0) {
        throw "生产配置缺少 MCP_ALLOWED_HOSTS。"
    }
    $mcpConnectHost = switch ($env:MCP_BIND_HOST) {
        "0.0.0.0" { "127.0.0.1" }
        "::" { "[::1]" }
        "localhost" { "127.0.0.1" }
        default { $env:MCP_BIND_HOST }
    }
    if (-not $mcpConnectHost) {
        throw "生产配置缺少 MCP_BIND_HOST。"
    }
    $mcpHealthUrl = "http://${mcpConnectHost}:${mcpPort}/healthz"

    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $rootBytes = [Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())
        $rootHash = [BitConverter]::ToString($sha256.ComputeHash($rootBytes)).Replace("-", "").Substring(0, 16)
    } finally {
        $sha256.Dispose()
    }
    $mutex = New-Object System.Threading.Mutex($false, "Local\ArticleOpsLauncher-$rootHash")
    try {
        $mutexAcquired = $mutex.WaitOne([TimeSpan]::FromSeconds($MutexTimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
        $mutexAcquired = $true
        Write-LauncherLog "检测到上次启动器异常结束，已接管启动锁。"
    }
    if (-not $mutexAcquired) {
        throw "另一个 ArticleOps 启动操作仍在进行，请稍后再试。"
    }

    $webOwner = Get-ListenerOwner -Port $flaskPort
    if ($webOwner -and -not (Test-CurrentInstallationProcess -Process $webOwner -Kind "Flask")) {
        throw "端口 $flaskPort 已被其他程序或其他 ArticleOps 目录占用（PID=$($webOwner.ProcessId)），未自动结束该进程。"
    }
    $mcpOwner = Get-ListenerOwner -Port $mcpPort
    if ($mcpOwner -and -not (Test-CurrentInstallationProcess -Process $mcpOwner -Kind "Mcp")) {
        throw "端口 $mcpPort 已被其他程序或其他 ArticleOps 目录占用（PID=$($mcpOwner.ProcessId)），未自动结束该进程。"
    }

    $webHealthy = $webOwner -and (Test-WebHealth -Url $healthUrl)
    $mcpHealthy = $mcpOwner -and (Test-McpHealth -Url $mcpHealthUrl -HostHeader $mcpHealthHost[0])
    if (-not ($webHealthy -and $mcpHealthy)) {

        # 仅停止当前安装目录中可确认的生产进程；正式启动脚本会再次校验端口。
        Invoke-ProjectScript -Name "stop_production_windows.ps1"
        Invoke-ProjectScript -Name "start_production_windows.ps1"

        $webOwner = Get-ListenerOwner -Port $flaskPort
        if (-not $webOwner -or -not (Test-CurrentInstallationProcess -Process $webOwner -Kind "Flask")) {
            throw "服务启动后未找到当前安装目录对应的 $flaskPort 监听进程。"
        }
        if (-not (Test-WebHealth -Url $healthUrl)) {
            throw "服务启动后健康检查仍未通过。"
        }
        $mcpOwner = Get-ListenerOwner -Port $mcpPort
        if (-not $mcpOwner -or -not (Test-CurrentInstallationProcess -Process $mcpOwner -Kind "Mcp")) {
            throw "服务启动后未找到当前安装目录对应的 $mcpPort 监听进程。"
        }
        if (-not (Test-McpHealth -Url $mcpHealthUrl -HostHeader $mcpHealthHost[0])) {
            throw "MCP 启动后健康检查仍未通过。"
        }
        Write-LauncherLog "服务已由桌面启动器恢复，Flask PID=$($webOwner.ProcessId)，MCP PID=$($mcpOwner.ProcessId)。"
    }

    if (-not $NoBrowser) {
        Start-Process -FilePath $WebUrl
    }
    Write-LauncherLog "管理页已就绪。"
} catch {
    $message = $_.Exception.Message
    Write-LauncherLog "启动失败：$message"
    Show-LauncherFailure -Message $message
    throw
} finally {
    if ($mutexAcquired -and $mutex) {
        $mutex.ReleaseMutex()
    }
    if ($mutex) {
        $mutex.Dispose()
    }
}
