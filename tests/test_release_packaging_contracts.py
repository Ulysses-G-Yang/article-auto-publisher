from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


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


def test_windows_start_script_exposes_bundled_src_modules_to_mcp() -> None:
    script = read("scripts/start_production_windows.ps1")

    assert '$srcRoot = Join-Path $ProjectRoot "src"' in script
    assert "$env:PYTHONPATH" in script
    assert '"$srcRoot;$($env:PYTHONPATH)"' in script


def test_release_docs_describe_runtime_only_test_behavior() -> None:
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")
    release = read("docs/releases/v0.4.1-rc1.md")

    assert "仍会执行 `compileall`" in docs
    assert "跳过不存在的 `pytest`" in docs
    assert "排除 `.git`、`data`、数据库、Cookie" in release
