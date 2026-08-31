from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def test_windows_powershell_scripts_have_utf8_bom_for_version_5_1() -> None:
    for relative_path in (
        "scripts/setup_windows.ps1",
        "scripts/start_production_windows.ps1",
        "scripts/stop_production_windows.ps1",
        "scripts/production_env.example.ps1",
        "scripts/build_release_windows.ps1",
        "scripts/apply_upgrade_windows.ps1",
    ):
        assert (ROOT / relative_path).read_bytes().startswith(b"\xef\xbb\xbf")


def test_windows_setup_selects_runtime_dependencies_for_source_packages() -> None:
    script = read("scripts/setup_windows.ps1")
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")

    assert '$requirementsFile = if ($gitRepository)' in script
    assert '"requirements-dev.txt"' in script
    assert '"requirements.txt"' in script
    assert '"python", "-m", "pip", "install", "-r", $requirementsFile' in script
    assert '$testsAvailable = Test-Path' in script
    assert '"当前发布包未包含 tests；跳过 pytest，compileall 已完成。"' in script
    assert '"-m", "compileall"' in script
    assert "requirements-dev.txt" in docs
    assert "requirements.txt" in docs


def test_windows_setup_keeps_all_mutating_paths_closed_by_default() -> None:
    script = read("scripts/setup_windows.ps1")
    example = read("scripts/production_env.example.ps1")

    for variable in (
        "PUBLISH_AFTER_DRAFT",
        "LEGACY_UPLOAD_QUEUE_ENABLED",
        "ACCOUNT_SESSIONS_ALLOW_PUBLIC_PUBLISH",
        "ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED",
        "MCP_LEGACY_MUTATIONS_ENABLED",
    ):
        expected = f"`$env:{variable} = $(ConvertTo-PowerShellLiteral 'false')"
        assert expected in script
        assert f"$env:{variable} = 'false'" in example

    deny_all_account_id = "00000000-0000-0000-0000-000000000000"
    assert (
        "`$env:ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS = "
        f"$(ConvertTo-PowerShellLiteral '{deny_all_account_id}')"
    ) in script
    assert (
        "$env:ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS = "
        f"'{deny_all_account_id}'"
    ) in example


def test_release_whitelist_excludes_development_and_machine_state_files() -> None:
    script = read("scripts/build_release_windows.ps1")

    assert '[string]$Version = "0.4.5"' in script
    assert '"requirements-dev.txt"' not in script
    assert '"tests"' not in script
    assert '"human"' in script
    for pattern in (".git", "data", "Cookie", "Profile", "logs", "node_modules", "uv\\.lock"):
        assert pattern in script
    assert "source_commit=$resolvedSha" in script
    assert "Get-FileHash -LiteralPath $archiveOutput -Algorithm SHA256" in script
    assert '"ArticleOps-upgrade-v$Version-$resolvedSha"' in script
    assert '"scripts\\apply_upgrade_windows.ps1"' in script
    assert '"MCP_API_REFERENCE.md"' in script
    assert '"docs\\releases\\v0.4.3-weibo-mcp.md"' in script
    assert '"docs\\releases\\v0.4.5-draft-evidence-stability.md"' in script
    assert '"docs\\deployment\\UPGRADE_v0.4.5.md"' in script
    assert '-DestinationRelativePath "RELEASE_NOTES_v0.4.5.md"' in script


def test_upgrade_payload_preserves_customer_deployment_scripts() -> None:
    script = read("scripts/build_release_windows.ps1")

    assert "$upgradeExcludedRuntimeScripts = @(" in script
    for relative_path in (
        r"scripts\setup_windows.ps1",
        r"scripts\start_production_windows.ps1",
        r"scripts\stop_production_windows.ps1",
        r"scripts\production_env.example.ps1",
    ):
        assert f'"{relative_path}"' in script
    assert "Remove-Item -LiteralPath $excludedPath -Force" in script
    assert "excluded_payload_files" in script
    assert '(Join-Path $SourceRoot "docs\\deployment\\UPGRADE_v0.4.5.md")' in script
    assert '(Join-Path $UpgradeRoot "UPGRADE_v0.4.5.md")' in script


def test_windows_setup_prints_explicit_production_start_sequence() -> None:
    script = read("scripts/setup_windows.ps1")

    load_environment = 'Write-Host "下一步 1/2：. .\\data\\production_env.ps1"'
    start_production = 'Write-Host "下一步 2/2：.\\scripts\\start_production_windows.ps1"'

    assert load_environment in script
    assert start_production in script
    assert script.index(load_environment) < script.index(start_production)


def test_v045_product_version_does_not_change_mcp_component_version() -> None:
    project = read("pyproject.toml")
    article_mvp = read("src/article_mvp/__init__.py")
    mcp_component = read("mcp_server/__init__.py")

    assert 'version = "0.4.5"' in project
    assert '__version__ = "0.4.5"' in article_mvp
    assert 'SERVER_VERSION = "1.1.0"' in mcp_component


def test_v044_upgrade_guide_requires_validate_only_and_rejects_replacement_setup() -> None:
    guide = read("docs/deployment/UPGRADE_v0.4.4.md")
    release = read("docs/releases/v0.4.4-draft-result-upgrade.md")

    assert "已有 ArticleOps v0.4.3 完整安装" in guide
    assert "不要重新运行 `setup_windows.ps1`" in guide
    assert "-ValidateOnly" in guide
    assert "不停止服务、不复制文件、也不修改" in guide
    assert "data\\upgrade_backups" in guide
    assert "载荷不覆盖" in release
    assert "ArticleOps-upgrade-v0.4.4-<source-sha>.zip" in guide
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File" in release


def test_windows_upgrade_preserves_runtime_state_and_rolls_back_code() -> None:
    script = read("scripts/apply_upgrade_windows.ps1")

    for preserved in ("data", "uploads", "images", "production_env.ps1"):
        assert preserved in script
    assert "UPGRADE_MANIFEST.json" in script
    assert "Get-FileHash" in script
    assert "$ValidateOnly" in script
    assert "仅校验模式未停止服务、未复制文件、未修改目标目录" in script
    assert "data\\upgrade_backups" in script
    assert 'Invoke-TargetScript -Name "stop_production_windows.ps1"' in script
    assert 'Invoke-TargetScript -Name "start_production_windows.ps1"' in script
    assert "已尝试恢复旧代码" in script


def test_windows_start_script_exposes_bundled_src_modules_to_mcp() -> None:
    script = read("scripts/start_production_windows.ps1")

    assert '$srcRoot = Join-Path $ProjectRoot "src"' in script
    assert "$env:PYTHONPATH" in script
    assert '"$srcRoot;$($env:PYTHONPATH)"' in script


def test_v045_release_docs_describe_runtime_only_test_behavior() -> None:
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")
    release = read("docs/releases/v0.4.3-weibo-mcp.md")

    assert "仍会执行 `compileall`" in docs
    assert "跳过不存在的 `pytest`" in docs
    assert "排除 `.git`、`data`、数据库、Cookie" in release
    assert "ArticleOps-upgrade-v0.4.5" in docs
    assert "data\\upgrade_backups" in docs
    assert "start_article_draft_delivery" in docs
