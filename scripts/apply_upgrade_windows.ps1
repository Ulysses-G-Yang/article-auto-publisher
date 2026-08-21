#Requires -Version 5.1

<##
.SYNOPSIS
    将 ArticleOps 代码补丁安全覆盖到已有 Windows 运行目录。

.DESCRIPTION
    仅更新清单中的程序文件。data、数据库、Cookie、Chrome Profile、上传内容、
    生产配置和历史记录均不会进入补丁，也不会被删除。更新失败时按文件级备份回滚。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRoot,

    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$PatchRoot = $PSScriptRoot
$PayloadRoot = Join-Path $PatchRoot "payload"
$ManifestPath = Join-Path $PatchRoot "UPGRADE_MANIFEST.json"
$PreservedRuntimePaths = @("data", "uploads", "images", "data\production_env.ps1")

function Get-SafeChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    if ([IO.Path]::IsPathRooted($RelativePath) -or $RelativePath -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "升级清单包含不安全路径：$RelativePath"
    }
    $normalizedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = [IO.Path]::GetFullPath((Join-Path $normalizedRoot $RelativePath))
    if (-not $candidate.StartsWith($normalizedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "升级路径越出目标目录：$RelativePath"
    }
    return $candidate
}

function Invoke-TargetScript {
    param([Parameter(Mandatory = $true)][string]$Name)

    $scriptPath = Join-Path $ResolvedTargetRoot "scripts\$Name"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "目标目录缺少脚本：scripts\$Name"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath
    if ($LASTEXITCODE -ne 0) {
        throw "脚本执行失败：$Name (exit=$LASTEXITCODE)"
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $PayloadRoot -PathType Container)) {
    throw "升级包不完整：缺少 payload 或 UPGRADE_MANIFEST.json。"
}

$ResolvedTargetRoot = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\', '/')
if ($ResolvedTargetRoot -eq [IO.Path]::GetPathRoot($ResolvedTargetRoot).TrimEnd('\', '/')) {
    throw "拒绝把磁盘根目录作为升级目标。"
}
foreach ($marker in @("app.py", "config.py", "scripts\start_production_windows.ps1", "scripts\stop_production_windows.ps1")) {
    if (-not (Test-Path -LiteralPath (Join-Path $ResolvedTargetRoot $marker) -PathType Leaf)) {
        throw "目标不是有效的 ArticleOps 运行目录，缺少：$marker"
    }
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if (-not $manifest.source_commit -or $manifest.source_commit -notmatch '^[0-9a-f]{40}$' -or -not $manifest.files) {
    throw "升级清单格式无效。"
}

$runtimePrefixes = @("data/", "uploads/", "images/")
foreach ($entry in $manifest.files) {
    $relativePath = ([string]$entry.path).Replace('\', '/')
    if ($runtimePrefixes | Where-Object { $relativePath.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }) {
        throw "升级清单非法包含运行数据：$relativePath"
    }
    $payloadPath = Get-SafeChildPath -Root $PayloadRoot -RelativePath $relativePath
    if (-not (Test-Path -LiteralPath $payloadPath -PathType Leaf)) {
        throw "升级载荷缺少文件：$relativePath"
    }
    $actualHash = (Get-FileHash -LiteralPath $payloadPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$entry.sha256).ToLowerInvariant()) {
        throw "升级载荷校验失败：$relativePath"
    }
}

if ($ValidateOnly) {
    Write-Host "升级包校验通过：source_commit=$($manifest.source_commit)" -ForegroundColor Green
    Write-Host "仅校验模式未停止服务、未复制文件、未修改目标目录。" -ForegroundColor Cyan
    return
}

$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$backupRoot = Join-Path $ResolvedTargetRoot "data\upgrade_backups\$timestamp"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
$state = @()
$requirementsTarget = Join-Path $ResolvedTargetRoot "requirements.txt"
$oldRequirementsHash = if (Test-Path -LiteralPath $requirementsTarget -PathType Leaf) {
    (Get-FileHash -LiteralPath $requirementsTarget -Algorithm SHA256).Hash.ToLowerInvariant()
} else { "" }

try {
    Invoke-TargetScript -Name "stop_production_windows.ps1"

    foreach ($entry in $manifest.files) {
        $relativePath = ([string]$entry.path).Replace('/', '\')
        $targetPath = Get-SafeChildPath -Root $ResolvedTargetRoot -RelativePath $relativePath
        $backupPath = Get-SafeChildPath -Root $backupRoot -RelativePath $relativePath
        $existed = Test-Path -LiteralPath $targetPath -PathType Leaf
        if ($existed) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupPath) | Out-Null
            Copy-Item -LiteralPath $targetPath -Destination $backupPath -Force
        }
        $state += [ordered]@{ path = $relativePath; existed = $existed }
    }
    $state | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $backupRoot "upgrade_state.json") -Encoding UTF8

    foreach ($entry in $manifest.files) {
        $relativePath = ([string]$entry.path).Replace('/', '\')
        $payloadPath = Get-SafeChildPath -Root $PayloadRoot -RelativePath $relativePath
        $targetPath = Get-SafeChildPath -Root $ResolvedTargetRoot -RelativePath $relativePath
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
        Copy-Item -LiteralPath $payloadPath -Destination $targetPath -Force
    }

    $newRequirementsHash = (Get-FileHash -LiteralPath $requirementsTarget -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($newRequirementsHash -ne $oldRequirementsHash) {
        $conda = Get-Command conda -ErrorAction SilentlyContinue
        if (-not $conda) { throw "requirements.txt 已变化，但找不到 conda。" }
        & $conda.Source run --no-capture-output -n article-publisher-py312 python -m pip install -r $requirementsTarget
        if ($LASTEXITCODE -ne 0) { throw "运行依赖升级失败。" }
    }

    Invoke-TargetScript -Name "start_production_windows.ps1"
    Write-Host "升级完成：source_commit=$($manifest.source_commit)" -ForegroundColor Green
    Write-Host "运行数据与账号 Profile 已保留；代码备份：$backupRoot" -ForegroundColor Cyan
} catch {
    $failure = $_
    try { Invoke-TargetScript -Name "stop_production_windows.ps1" } catch { }
    foreach ($item in $state) {
        $targetPath = Get-SafeChildPath -Root $ResolvedTargetRoot -RelativePath $item.path
        $backupPath = Get-SafeChildPath -Root $backupRoot -RelativePath $item.path
        if ($item.existed) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetPath) | Out-Null
            Copy-Item -LiteralPath $backupPath -Destination $targetPath -Force
        } elseif (Test-Path -LiteralPath $targetPath -PathType Leaf) {
            Remove-Item -LiteralPath $targetPath -Force
        }
    }
    try { Invoke-TargetScript -Name "start_production_windows.ps1" } catch { }
    throw "升级失败，已尝试恢复旧代码。原始错误：$($failure.Exception.Message)"
}
