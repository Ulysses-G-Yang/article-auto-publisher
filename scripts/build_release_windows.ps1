#Requires -Version 5.1

<##
.SYNOPSIS
    从一个精确的 Git commit 构建 ArticleOps Windows RC 发布包。

.DESCRIPTION
    构建过程先用 git archive 固定源码快照，再按白名单复制运行代码、Web
    生产资产、运行依赖、Windows 启停脚本、配置模板和发布说明。不会把
    .git、data、数据库、Cookie、Profile、日志、测试、QA 截图、node_modules
    或 uv.lock 放进包内。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$CommitSha,

    [string]$OutputDirectory = ".\build\release",

    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$')]
    [string]$Version = "0.4.3"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $result = & git @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Git 命令失败：git $($Arguments -join ' ')`n$($result -join [Environment]::NewLine)"
    }
    return $result
}

function Copy-TrackedFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$DestinationRelativePath
    )

    $source = Join-Path $SourceRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "发布白名单文件不存在：$RelativePath"
    }
    $destination = Join-Path $PackageRoot $DestinationRelativePath
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

function Copy-TrackedDirectory {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $source = Join-Path $SourceRoot $RelativePath
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        throw "发布白名单目录不存在：$RelativePath"
    }
    $destination = Join-Path $PackageRoot $RelativePath
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Copy-Item -LiteralPath $source -Destination $parent -Recurse -Force
}

function Copy-LicenseFile {
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Name
    )

    Copy-TrackedFile -RelativePath $RelativePath -DestinationRelativePath (Join-Path "licenses" $Name)
}

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCommand) {
    throw "找不到 git；RC 包必须从精确 commit 构建。"
}

$commitRevision = $CommitSha + '^{commit}'
$resolvedSha = (Invoke-Git -Arguments @("rev-parse", "--verify", $commitRevision) | Select-Object -Last 1).ToString().Trim().ToLowerInvariant()
if ($resolvedSha -notmatch '^[0-9a-f]{40}$' -or $resolvedSha -ne $CommitSha.ToLowerInvariant()) {
    throw "CommitSha 不是可验证的精确 40 位 commit：$CommitSha"
}

$outputRoot = [IO.Path]::GetFullPath((Join-Path (Get-Location).Path $OutputDirectory))
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("articleops-release-" + [guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $tempRoot "source.zip"
$extractRoot = Join-Path $tempRoot "source"
$SourceRoot = Join-Path $extractRoot "source"
$PackageRoot = Join-Path $tempRoot "package"
$UpgradeRoot = Join-Path $tempRoot "upgrade"
$packageName = "ArticleOps-v$Version-$resolvedSha"
$archiveOutput = Join-Path $outputRoot "$packageName.zip"
$hashOutput = "$archiveOutput.sha256"
$upgradeName = "ArticleOps-upgrade-v$Version-$resolvedSha"
$upgradeOutput = Join-Path $outputRoot "$upgradeName.zip"
$upgradeHashOutput = "$upgradeOutput.sha256"

try {
    New-Item -ItemType Directory -Force -Path $tempRoot, $extractRoot, $PackageRoot, $UpgradeRoot | Out-Null
    Invoke-Git -Arguments @("archive", "--format=zip", "--output=$archivePath", "--prefix=source/", $resolvedSha) | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "Git archive 解压后缺少 source 根目录。"
    }

    $rootFiles = @(
        "app.py",
        "config.py",
        "run_flask_production.py",
        "requirements.txt",
        "pyproject.toml",
        "THIRD_PARTY_NOTICES.md",
        "MCP_API_REFERENCE.md"
    )
    foreach ($relativePath in $rootFiles) {
        Copy-TrackedFile -RelativePath $relativePath -DestinationRelativePath $relativePath
    }

    $runtimeDirectories = @("core", "human", "mcp_server", "models", "platforms", "src", "web")
    foreach ($relativePath in $runtimeDirectories) {
        Copy-TrackedDirectory -RelativePath $relativePath
    }

    $runtimeScripts = @(
        "scripts\check_environment.py",
        "scripts\setup_windows.ps1",
        "scripts\start_production_windows.ps1",
        "scripts\stop_production_windows.ps1",
        "scripts\production_env.example.ps1"
    )
    foreach ($relativePath in $runtimeScripts) {
        Copy-TrackedFile -RelativePath $relativePath -DestinationRelativePath $relativePath
    }

    $releaseDocuments = @(
        "docs\releases\v0.4.0-draft-delivery.md",
        "docs\releases\v0.4.1-rc1.md",
        "docs\releases\v0.4.2-hotfix.md",
        "docs\releases\v0.4.3-weibo-mcp.md",
        "docs\deployment\PRODUCTION_WINDOWS.md"
    )
    foreach ($relativePath in $releaseDocuments) {
        Copy-TrackedFile -RelativePath $relativePath -DestinationRelativePath $relativePath
    }

    Copy-LicenseFile -RelativePath "frontend\coreui-free-bootstrap-admin-template\LICENSE" -Name "COREUI_TEMPLATE_LICENSE"
    Copy-LicenseFile -RelativePath "web\static\vendor\coreui-template\LICENSE" -Name "COREUI_VENDOR_LICENSE"
    Copy-LicenseFile -RelativePath "src\article_mvp\web\static\vendor\coreui\LICENSE.txt" -Name "COREUI_LICENSE.txt"
    Copy-LicenseFile -RelativePath "src\article_mvp\web\static\vendor\coreui-icons\LICENSE.txt" -Name "COREUI_ICONS_LICENSE.txt"
    Copy-LicenseFile -RelativePath "src\article_mvp\web\static\vendor\gridstack\LICENSE.txt" -Name "GRIDSTACK_LICENSE.txt"

    $manifest = @(
        "ArticleOps v$Version",
        "source_commit=$resolvedSha",
        "built_at_utc=$([DateTime]::UtcNow.ToString('o'))",
        "public_publish_default=false",
        "runtime_data=created by setup_windows.ps1; not included",
        "source_tests=excluded",
        "source_qa=excluded"
    )
    Set-Content -LiteralPath (Join-Path $PackageRoot "RELEASE_MANIFEST.txt") -Value $manifest -Encoding UTF8

    $forbiddenEntryPatterns = @(
        '(?i)(^|[\\/])\.git([\\/]|$)',
        '(?i)(^|[\\/])data([\\/]|$)',
        '(?i)(^|[\\/])(Cookies?|Profile|chrome_profiles)([\\/]|$)',
        '(?i)(^|[\\/])logs?([\\/]|$)',
        '(?i)\.log$',
        '(?i)(^|[\\/])[^\\/]+\.(db|sqlite|sqlite3)$',
        '(?i)(^|[\\/])(tests?|qa)([\\/]|$)',
        '(?i)(^|[\\/])node_modules([\\/]|$)',
        '(?i)(^|[\\/])uv\.lock$'
    )
    $entries = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force | ForEach-Object {
        $_.FullName.Substring($PackageRoot.Length).TrimStart([char[]]@('\', '/'))
    })
    foreach ($entry in $entries) {
        foreach ($pattern in $forbiddenEntryPatterns) {
            if ($entry -match $pattern) {
                throw "发布包命中禁止路径：$entry"
            }
        }
    }

    # 运行包内不应出现开发机盘符或 UNC 路径；运行时目录由 setup 脚本按
    # 当前机器动态解析，发布说明只使用相对路径。
    $textExtensions = @(".py", ".ps1", ".md", ".txt", ".yml", ".yaml", ".html", ".css", ".js", ".json")
    foreach ($file in Get-ChildItem -LiteralPath $PackageRoot -Recurse -File) {
        if ($textExtensions -notcontains $file.Extension.ToLowerInvariant()) { continue }
        $text = Get-Content -LiteralPath $file.FullName -Raw
        if ($text -match '(?i)(?<![A-Za-z0-9])([A-Z]:\\|\\\\(?!u[0-9a-f]{4}\\)[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+)') {
            throw "发布包文本包含本机绝对路径：$($file.FullName.Substring($PackageRoot.Length))"
        }
    }

    Compress-Archive -Path (Join-Path $PackageRoot "*") -DestinationPath $archiveOutput -Force
    $hash = (Get-FileHash -LiteralPath $archiveOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $hashOutput -Value "$hash  $([IO.Path]::GetFileName($archiveOutput))" -Encoding ASCII

    $upgradePayloadRoot = Join-Path $UpgradeRoot "payload"
    New-Item -ItemType Directory -Force -Path $upgradePayloadRoot | Out-Null
    Copy-Item -Path (Join-Path $PackageRoot "*") -Destination $upgradePayloadRoot -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts\apply_upgrade_windows.ps1") `
        -Destination (Join-Path $UpgradeRoot "apply_upgrade_windows.ps1") -Force

    $upgradeFiles = @(Get-ChildItem -LiteralPath $upgradePayloadRoot -Recurse -File | ForEach-Object {
        [ordered]@{
            path = $_.FullName.Substring($upgradePayloadRoot.Length).TrimStart([char[]]@('\', '/')).Replace('\', '/')
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    })
    $upgradeManifest = [ordered]@{
        version = $Version
        source_commit = $resolvedSha
        built_at_utc = [DateTime]::UtcNow.ToString('o')
        preserves = @("data", "uploads", "images", "Cookie", "Chrome Profile", "production_env.ps1")
        files = $upgradeFiles
    }
    $upgradeManifest | ConvertTo-Json -Depth 5 | Set-Content `
        -LiteralPath (Join-Path $UpgradeRoot "UPGRADE_MANIFEST.json") -Encoding UTF8
    Compress-Archive -Path (Join-Path $UpgradeRoot "*") -DestinationPath $upgradeOutput -Force
    $upgradeHash = (Get-FileHash -LiteralPath $upgradeOutput -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $upgradeHashOutput `
        -Value "$upgradeHash  $([IO.Path]::GetFileName($upgradeOutput))" -Encoding ASCII

    Write-Host "完整包：$archiveOutput" -ForegroundColor Green
    Write-Host "完整包 SHA256：$hashOutput" -ForegroundColor Green
    Write-Host "升级包：$upgradeOutput" -ForegroundColor Green
    Write-Host "升级包 SHA256：$upgradeHashOutput" -ForegroundColor Green
    Write-Host "source_commit=$resolvedSha" -ForegroundColor Cyan
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
