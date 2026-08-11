#Requires -Version 5.1

<##
.SYNOPSIS
    在一台全新的 Windows 电脑上初始化文章发布工具。

.DESCRIPTION
    该脚本只创建当前项目的运行目录和 Python 环境，不复制任何测试数据库、
    Cookie、Chrome Profile 或上传文件。它要求电脑已安装 Git、Conda 和 Google Chrome。
    生产环境配置会写入被 .gitignore 忽略的 data\production_env.ps1。
#>
[CmdletBinding()]
param(
    [string]$EnvironmentName = "article-publisher-py312",
    [string]$ExpectedCommit = "",
    [string]$McpBindHost = "",
    [string]$McpAllowedHosts = "",
    [string]$FileServiceHost = "dev.sccsai.com",
    [switch]$ForceConfig,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（exit=$LASTEXITCODE）: $FilePath $($Arguments -join ' ')"
    }
}

function Get-CommandPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "找不到 $Name。请先安装并加入 PATH 后重新运行。"
    }
    if ($command.Source -and (Test-Path -LiteralPath $command.Source)) {
        return $command.Source
    }
    return $command.Name
}

function ConvertTo-PowerShellLiteral {
    param([AllowEmptyString()][string]$Value)

    return "'" + (($Value -as [string]) -replace "'", "''") + "'"
}

Write-Host "[1/6] 检查项目版本和系统前置条件" -ForegroundColor Cyan
$gitRepository = Test-Path -LiteralPath (Join-Path $ProjectRoot ".git")
if ($gitRepository) {
    $GitCommand = Get-CommandPath "git"
} else {
    Write-Warning "当前是源码压缩包模式，未检测到 .git；跳过 Git commit 校验。"
}
$CondaCommand = Get-CommandPath "conda"

if ($gitRepository) {
    $currentCommit = (& $GitCommand rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取当前 Git commit。"
    }
    if ($ExpectedCommit -and $currentCommit -ne $ExpectedCommit) {
        throw "当前代码不是已验证发行 commit。期望 $ExpectedCommit，实际 $currentCommit。请先 checkout 正确版本，或显式传入 -ExpectedCommit。"
    }
} else {
    $currentCommit = "source-package"
}
Write-Host "代码版本: $currentCommit" -ForegroundColor Green

$chromeCandidates = @()
foreach ($basePath in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if ($basePath) {
        $chromeCandidates += (Join-Path $basePath "Google\Chrome\Application\chrome.exe")
    }
}
$chromePath = $chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $chromePath) {
    throw "找不到 Google Chrome。请先安装 Chrome Stable；本项目使用系统 Chrome 的 channel=chrome。"
}
Write-Host "Chrome: $chromePath" -ForegroundColor Green

$condaBase = ((& $CondaCommand info --base) | Select-Object -Last 1).ToString().Trim()
if ($LASTEXITCODE -ne 0 -or -not $condaBase) {
    throw "无法获取 Conda 根目录。"
}
$pythonPath = Join-Path $condaBase "envs\$EnvironmentName\python.exe"

Write-Host "[2/6] 创建或检查 Python 3.12 环境" -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Invoke-Native $CondaCommand @("create", "-n", $EnvironmentName, "python=3.12", "-y")
}
$pythonVersion = (& $pythonPath --version 2>&1).ToString().Trim()
if ($pythonVersion -notmatch "^Python 3\.12\.") {
    throw "环境 $EnvironmentName 不是 Python 3.12：$pythonVersion"
}
Write-Host $pythonVersion -ForegroundColor Green

Write-Host "[3/6] 安装固定运行和测试依赖" -ForegroundColor Cyan
Invoke-Native $CondaCommand @(
    "run", "--no-capture-output", "-n", $EnvironmentName,
    "python", "-m", "pip", "install", "-r", "requirements-dev.txt"
)

Write-Host "[4/6] 创建全新的生产运行目录" -ForegroundColor Cyan
foreach ($relativePath in @("data", "data\logs", "data\chrome_profiles", "uploads", "images")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot $relativePath) | Out-Null
}

$envFile = Join-Path $ProjectRoot "data\production_env.ps1"
if ((Test-Path -LiteralPath $envFile) -and -not $ForceConfig) {
    Write-Host "保留已有生产配置: $envFile" -ForegroundColor Yellow
} else {
    if (-not $McpBindHost) {
        $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.AddressState -eq "Preferred" -and
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*"
            } |
            Select-Object -First 1
        if (-not $candidate) {
            throw "无法自动检测局域网 IPv4。请使用 -McpBindHost 10.x.x.x 重新运行。"
        }
        $McpBindHost = $candidate.IPAddress
    }

    $allowedHosts = @($McpBindHost, "localhost", "127.0.0.1")
    if ($McpAllowedHosts) {
        $allowedHosts += $McpAllowedHosts -split ","
    }
    $allowedHosts = $allowedHosts |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ } |
        Select-Object -Unique

    $secret = (& $pythonPath -c "import secrets; print(secrets.token_urlsafe(48))").Trim()
    if ($LASTEXITCODE -ne 0 -or $secret.Length -lt 32) {
        throw "无法生成生产 APP_SECRET_KEY。"
    }

    $configLines = @(
        "`$env:APP_ENV = $(ConvertTo-PowerShellLiteral 'production')",
        "`$env:APP_SECRET_KEY = $(ConvertTo-PowerShellLiteral $secret)",
        "`$env:APP_DEBUG = $(ConvertTo-PowerShellLiteral 'false')",
        "`$env:FLASK_HOST = $(ConvertTo-PowerShellLiteral '127.0.0.1')",
        "`$env:FLASK_PORT = $(ConvertTo-PowerShellLiteral '5000')",
        "`$env:FLASK_BASE_URL = $(ConvertTo-PowerShellLiteral 'http://127.0.0.1:5000')",
        "`$env:MCP_BIND_HOST = $(ConvertTo-PowerShellLiteral $McpBindHost)",
        "`$env:MCP_PORT = $(ConvertTo-PowerShellLiteral '8765')",
        "`$env:MCP_ALLOWED_HOSTS = $(ConvertTo-PowerShellLiteral ($allowedHosts -join ','))",
        "`$env:MCP_FILE_SERVICE_ALLOWED_HOSTS = $(ConvertTo-PowerShellLiteral $FileServiceHost)",
        "`$env:PUBLISH_AFTER_DRAFT = $(ConvertTo-PowerShellLiteral 'false')"
    )
    Set-Content -LiteralPath $envFile -Value ($configLines -join [Environment]::NewLine) -Encoding UTF8
    Write-Host "已生成生产配置（密钥不会打印，也不会进入 Git）: $envFile" -ForegroundColor Green
}

. $envFile

Write-Host "[5/6] 检查生产配置和目录" -ForegroundColor Cyan
Invoke-Native $pythonPath @("scripts\check_environment.py", "--flask-port", "5000", "--mcp-port", "8765")

if (-not $SkipTests) {
    Write-Host "[6/6] 执行安装后的回归检查" -ForegroundColor Cyan
    Invoke-Native $pythonPath @("-m", "compileall", "-q", "app.py", "config.py", "core", "mcp_server", "platforms", "web", "scripts", "run_flask_production.py")
    Invoke-Native $pythonPath @("-m", "pytest", "-q")
} else {
    Write-Host "[6/6] 已跳过 pytest（-SkipTests）" -ForegroundColor Yellow
}

Write-Host "初始化完成。" -ForegroundColor Green
Write-Host "下一步启动：.\scripts\start_production_windows.ps1" -ForegroundColor Cyan
Write-Host "管理页（生产机本机）：http://127.0.0.1:5000" -ForegroundColor Cyan
Write-Host "MCP：http://$($env:MCP_BIND_HOST):$($env:MCP_PORT)/mcp" -ForegroundColor Cyan
