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


def test_release_whitelist_excludes_development_and_machine_state_files() -> None:
    script = read("scripts/build_release_windows.ps1")

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


def test_release_docs_describe_runtime_only_test_behavior() -> None:
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")
    release = read("docs/releases/v0.4.3-weibo-mcp.md")

    assert "仍会执行 `compileall`" in docs
    assert "跳过不存在的 `pytest`" in docs
    assert "排除 `.git`、`data`、数据库、Cookie" in release
    assert "ArticleOps-upgrade-v0.4.3" in docs
    assert "data\\upgrade_backups" in docs
    assert "start_article_draft_delivery" in docs
