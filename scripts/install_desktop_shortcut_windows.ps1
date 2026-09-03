#Requires -Version 5.1

<##
.SYNOPSIS
    为当前 Windows 用户创建 ArticleOps 桌面快捷方式。
#>
[CmdletBinding()]
param(
    [string]$DestinationDirectory = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LauncherPath = Join-Path $ProjectRoot "scripts\launch_articleops_windows.ps1"

if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "缺少桌面启动器：scripts\launch_articleops_windows.ps1"
}

if (-not $DestinationDirectory) {
    $DestinationDirectory = [Environment]::GetFolderPath("DesktopDirectory")
}
if (-not $DestinationDirectory) {
    throw "无法确定当前用户桌面目录。"
}
New-Item -ItemType Directory -Force -Path $DestinationDirectory | Out-Null
$ResolvedDestination = (Resolve-Path -LiteralPath $DestinationDirectory).Path

$PowerShellPath = Join-Path $PSHOME "powershell.exe"
if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "找不到 Windows PowerShell：$PowerShellPath"
}

$ShortcutPath = Join-Path $ResolvedDestination "ArticleOps 创作与投递.lnk"
$shell = New-Object -ComObject WScript.Shell
try {
    $shortcut = $shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $PowerShellPath
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LauncherPath`""
    $shortcut.WorkingDirectory = $ProjectRoot
    $shortcut.Description = "打开 ArticleOps；服务停止时自动尝试恢复"
    $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
    $shortcut.Save()
} finally {
    if ($shortcut) {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut)
    }
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}

if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
    throw "桌面快捷方式创建失败。"
}
Write-Host "桌面快捷方式已创建：$ShortcutPath" -ForegroundColor Green
