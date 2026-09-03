import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8-sig")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"test server did not listen on {port}")


def _write_launcher_fixture(root: Path, flask_port: int, mcp_port: int) -> Path:
    scripts = root / "scripts"
    data = root / "data"
    scripts.mkdir(parents=True)
    data.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "launch_articleops_windows.ps1", scripts)
    (data / "production_env.ps1").write_text(
        "\n".join(
            (
                "$env:APP_ENV = 'production'",
                f"$env:FLASK_PORT = '{flask_port}'",
                f"$env:MCP_PORT = '{mcp_port}'",
                "$env:MCP_BIND_HOST = '127.0.0.1'",
                "$env:MCP_ALLOWED_HOSTS = '127.0.0.1'",
            )
        ),
        encoding="utf-8-sig",
    )
    return scripts / "launch_articleops_windows.ps1"


def _start_test_http_process(script_path: Path, port: int) -> subprocess.Popen:
    script_path.write_text(
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "import sys\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'ok')\n"
        "    def log_message(self, format, *args):\n"
        "        pass\n"
        "ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)"
        ".serve_forever()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(script_path), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_port(port)
    return process


def _prepare_upgrade_fixture(tmp_path: Path, *, failing_start: bool = False):
    patch_root = tmp_path / "patch"
    payload_root = patch_root / "payload"
    target_root = tmp_path / "target"
    target_scripts = target_root / "scripts"
    payload_root.mkdir(parents=True)
    target_scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "apply_upgrade_windows.ps1", patch_root)

    (target_root / "app.py").write_text("old app\n", encoding="utf-8")
    (target_root / "config.py").write_text("config\n", encoding="utf-8")
    (target_root / "requirements.txt").write_text("same\n", encoding="utf-8")
    target_data = target_root / "data"
    target_data.mkdir()
    (target_data / "production_env.ps1").write_text(
        "\n".join(
            (
                "$env:APP_ENV = 'production'",
                "$env:APP_SECRET_KEY = 'test-secret-key-with-at-least-32-characters'",
                "$env:MCP_BIND_HOST = '127.0.0.1'",
                "$env:MCP_PORT = '8765'",
                "$env:FLASK_BASE_URL = 'http://127.0.0.1:5000'",
                "$env:ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED = 'false'",
                "$env:ARTICLEOPS_MCP_INTERNAL_TOKEN = 'test-token'",
                "$env:ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS = 'deny-all'",
                "$env:MCP_ALLOWED_HOSTS = '127.0.0.1'",
            )
        ),
        encoding="utf-8-sig",
    )
    for script_name in ("start_production_windows.ps1", "stop_production_windows.ps1"):
        (target_scripts / script_name).write_text("exit 0\n", encoding="ascii")

    retired_file = (
        target_root / "src" / "article_mvp" / "web" / "static" / "dashboard.js"
    )
    retired_file.parent.mkdir(parents=True)
    retired_file.write_text("retired dashboard\n", encoding="utf-8")

    payload_files = {
        "app.py": "new app\n",
        "requirements.txt": "same\n",
    }
    if failing_start:
        payload_files["scripts/start_production_windows.ps1"] = "exit 17\n"

    manifest_files = []
    for relative_path, content in payload_files.items():
        payload_path = payload_root / Path(relative_path)
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_text(content, encoding="utf-8")
        manifest_files.append({"path": relative_path, "sha256": _sha256(payload_path)})

    manifest = {
        "source_commit": "a" * 40,
        "files": manifest_files,
        "removed_files": ["src/article_mvp/web/static/dashboard.js"],
    }
    (patch_root / "UPGRADE_MANIFEST.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return patch_root, target_root, retired_file


def test_windows_powershell_scripts_have_utf8_bom_for_version_5_1() -> None:
    for relative_path in (
        "scripts/install_desktop_shortcut_windows.ps1",
        "scripts/launch_articleops_windows.ps1",
        "scripts/setup_windows.ps1",
        "scripts/start_production_windows.ps1",
        "scripts/stop_production_windows.ps1",
        "scripts/production_env.example.ps1",
        "scripts/build_release_windows.ps1",
        "scripts/apply_upgrade_windows.ps1",
    ):
        assert (ROOT / relative_path).read_bytes().startswith(b"\xef\xbb\xbf")


def test_release_scripts_explicitly_load_hashing_module() -> None:
    for relative_path in (
        "scripts/build_release_windows.ps1",
        "scripts/apply_upgrade_windows.ps1",
    ):
        assert "Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop" in read(
            relative_path
        )


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


def test_runtime_environment_check_does_not_require_dev_only_pytest() -> None:
    checker = read("scripts/check_environment.py")

    required_modules = checker.split("REQUIRED_MODULES = (", 1)[1].split(")", 1)[0]
    assert '"pytest"' not in required_modules


def test_source_package_expected_commit_is_checked_from_release_manifest() -> None:
    script = read("scripts/setup_windows.ps1")
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")

    assert 'Join-Path $ProjectRoot "RELEASE_MANIFEST.txt"' in script
    assert "'^source_commit='" in script
    assert "$manifestCommitLines.Count -ne 1" in script
    assert "'^[0-9a-f]{40}$'" in script
    assert "$ExpectedCommit.ToLowerInvariant()" in script
    assert "RELEASE_MANIFEST.txt" in docs
    assert "-ExpectedCommit" in docs


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

    assert '[string]$Version = "0.4.6"' in script
    assert '"requirements-dev.txt"' not in script
    assert '"tests"' not in script
    assert '"human"' in script
    for pattern in (".git", "data", "Cookie", "Profile", "logs", "node_modules", "uv\\.lock"):
        assert pattern in script
    assert "source_commit=$resolvedSha" in script
    assert "Get-FileHash -LiteralPath $archiveOutput -Algorithm SHA256" in script
    assert '"ArticleOps-upgrade-v$Version-$resolvedSha"' in script
    assert '"scripts\\apply_upgrade_windows.ps1"' in script
    assert '"scripts\\install_desktop_shortcut_windows.ps1"' in script
    assert '"scripts\\launch_articleops_windows.ps1"' in script
    assert '"MCP_API_REFERENCE.md"' in script
    assert '"docs\\releases\\v0.4.3-weibo-mcp.md"' in script
    assert '"docs\\releases\\v0.4.5-draft-evidence-stability.md"' in script
    assert '"docs\\releases\\v0.4.6-installer-integrity.md"' in script
    assert '"docs\\deployment\\UPGRADE_v0.4.6.md"' in script
    assert '-DestinationRelativePath "RELEASE_NOTES_v0.4.6.md"' in script


def test_release_builder_rejects_version_drift() -> None:
    script = read("scripts/build_release_windows.ps1")

    assert 'Join-Path $SourceRoot "pyproject.toml"' in script
    assert "if ($projectVersion -ne $Version)" in script
    assert 'Join-Path $SourceRoot "src\\article_mvp\\__init__.py"' in script
    assert "$projectSectionMatch.Groups['body'].Value" in script
    assert "$projectVersionMatches.Count -ne 1" in script
    assert "$packageVersionMatches.Count -ne 1" in script
    assert "$packageVersionMatches[0].Groups[1].Value -ne $Version" in script


def test_release_builder_version_drift_fails_without_artifacts(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is unavailable")

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "build_release_windows.ps1"),
            "-CommitSha",
            commit,
            "-Version",
            "9.9.9",
            "-OutputDirectory",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert not list(tmp_path.glob("*.zip"))
    assert not list(tmp_path.glob("*.sha256"))


def test_source_package_rejects_duplicate_manifest_commits(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    conda = shutil.which("conda.exe") or shutil.which("conda")
    if not powershell or not conda:
        pytest.skip("PowerShell or Conda is unavailable")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(ROOT / "scripts" / "setup_windows.ps1", scripts_dir)
    expected_commit = "0" * 40
    (tmp_path / "RELEASE_MANIFEST.txt").write_text(
        f"source_commit={expected_commit}\nsource_commit={'1' * 40}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts_dir / "setup_windows.ps1"),
            "-ExpectedCommit",
            expected_commit,
        ],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert "source_commit" in (result.stdout + result.stderr)
    assert not (tmp_path / "data").exists()


def test_upgrade_payload_preserves_setup_but_updates_runtime_launchers() -> None:
    script = read("scripts/build_release_windows.ps1")

    assert "$upgradeExcludedRuntimeScripts = @(" in script
    excluded_block = script.split("$upgradeExcludedRuntimeScripts = @(", 1)[1].split(
        ")",
        1,
    )[0]
    for relative_path in (
        r"scripts\setup_windows.ps1",
        r"scripts\production_env.example.ps1",
    ):
        assert f'"{relative_path}"' in excluded_block
    assert '"scripts\\start_production_windows.ps1"' not in excluded_block
    assert '"scripts\\stop_production_windows.ps1"' not in excluded_block
    assert "Remove-Item -LiteralPath $excludedPath -Force" in script
    assert "excluded_payload_files" in script
    assert '(Join-Path $SourceRoot "docs\\deployment\\UPGRADE_v0.4.6.md")' in script
    assert '(Join-Path $UpgradeRoot "UPGRADE_v0.4.6.md")' in script


def test_upgrade_manifest_lists_retired_dashboard_files() -> None:
    script = read("scripts/build_release_windows.ps1")

    assert "$upgradeRemovedFiles = @(" in script
    assert '"src\\article_mvp\\web\\static\\dashboard.js"' in script
    assert '"src\\article_mvp\\web\\templates\\dashboard.html"' in script
    assert "removed_files = @($upgradeRemovedFiles" in script


def test_windows_setup_installs_business_desktop_shortcut() -> None:
    script = read("scripts/setup_windows.ps1")

    assert 'Write-Host "[7/7] 创建业务人员桌面快捷方式"' in script
    assert '"scripts\\install_desktop_shortcut_windows.ps1"' in script
    assert "业务人员下一步：双击桌面的『ArticleOps 创作与投递』" in script


def test_v046_product_version_does_not_change_mcp_component_version() -> None:
    project = read("pyproject.toml")
    article_mvp = read("src/article_mvp/__init__.py")
    mcp_component = read("mcp_server/__init__.py")

    assert 'version = "0.4.6"' in project
    assert '__version__ = "0.4.6"' in article_mvp
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
    assert 'Invoke-TargetScript -Name "install_desktop_shortcut_windows.ps1"' in script
    assert 'Join-Path $ResolvedTargetRoot "data\\production_env.ps1"' in script
    assert "服务尚未停止" in script
    assert "旧代码已恢复，但旧服务重启失败" in script
    assert 'operation = "remove"' in script
    assert "Remove-Item -LiteralPath $targetPath -Force" in script
    assert "已恢复旧代码并重启旧服务" in script


def test_windows_upgrade_removes_retired_files_after_backup(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    patch_root, target_root, retired_file = _prepare_upgrade_fixture(tmp_path)

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(patch_root / "apply_upgrade_windows.ps1"),
            "-TargetRoot",
            str(target_root),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not retired_file.exists()
    assert (target_root / "app.py").read_text(encoding="utf-8-sig") == "new app\n"
    backup_files = list(
        (target_root / "data" / "upgrade_backups").glob(
            "*/src/article_mvp/web/static/dashboard.js"
        )
    )
    assert len(backup_files) == 1
    assert backup_files[0].read_text(encoding="utf-8-sig") == "retired dashboard\n"


def test_windows_upgrade_rejects_missing_environment_before_stop(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    patch_root, target_root, _ = _prepare_upgrade_fixture(tmp_path)
    (target_root / "data" / "production_env.ps1").unlink()
    child_environment = dict(os.environ)
    for name in (
        "APP_ENV",
        "APP_SECRET_KEY",
        "MCP_BIND_HOST",
        "MCP_PORT",
        "FLASK_BASE_URL",
        "ARTICLEOPS_MCP_DRAFT_DELIVERY_ENABLED",
        "ARTICLEOPS_MCP_INTERNAL_TOKEN",
        "ARTICLEOPS_MCP_ALLOWED_ACCOUNT_IDS",
        "MCP_ALLOWED_HOSTS",
    ):
        child_environment.pop(name, None)

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(patch_root / "apply_upgrade_windows.ps1"),
            "-TargetRoot",
            str(target_root),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=child_environment,
    )

    assert result.returncode != 0
    assert (target_root / "app.py").read_text(encoding="utf-8-sig") == "old app\n"
    assert not (target_root / "data" / "upgrade_backups").exists()


def test_windows_upgrade_restores_removed_files_on_failure(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")
    patch_root, target_root, retired_file = _prepare_upgrade_fixture(
        tmp_path,
        failing_start=True,
    )

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(patch_root / "apply_upgrade_windows.ps1"),
            "-TargetRoot",
            str(target_root),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode != 0
    assert (target_root / "app.py").read_text(encoding="utf-8-sig") == "old app\n"
    assert retired_file.read_text(encoding="utf-8-sig") == "retired dashboard\n"
    assert (
        target_root / "scripts" / "start_production_windows.ps1"
    ).read_text(encoding="utf-8-sig") == "exit 0\n"


def test_windows_start_script_exposes_bundled_src_modules_to_mcp() -> None:
    script = read("scripts/start_production_windows.ps1")

    assert '$srcRoot = Join-Path $ProjectRoot "src"' in script
    assert "$env:PYTHONPATH" in script
    assert '"$srcRoot;$($env:PYTHONPATH)"' in script
    assert '"0.0.0.0" { "127.0.0.1" }' in script
    assert '"::" { "[::1]" }' in script
    assert 'Host = $probeHostHeader[0]' in script


def test_windows_desktop_launcher_recovers_only_current_installation() -> None:
    launcher = read("scripts/launch_articleops_windows.ps1")

    assert 'Join-Path $ProjectRoot "data\\production_env.ps1"' in launcher
    assert '"http://127.0.0.1:${flaskPort}/upload"' in launcher
    assert '"http://127.0.0.1:${flaskPort}/api/status"' in launcher
    assert 'New-Object System.Threading.Mutex' in launcher
    assert 'Invoke-ProjectScript -Name "stop_production_windows.ps1"' in launcher
    assert 'Invoke-ProjectScript -Name "start_production_windows.ps1"' in launcher
    assert "$commandLine.Contains($ProjectRootMarker)" in launcher
    assert "未自动结束该进程" in launcher
    assert 'Start-Process -FilePath $WebUrl' in launcher


def test_windows_shortcut_installer_targets_the_resilient_launcher() -> None:
    installer = read("scripts/install_desktop_shortcut_windows.ps1")

    assert "DesktopDirectory" in installer
    assert "ArticleOps 创作与投递.lnk" in installer
    assert "WScript.Shell" in installer
    assert 'Join-Path $ProjectRoot "scripts\\launch_articleops_windows.ps1"' in installer
    assert "$shortcut.TargetPath = $PowerShellPath" in installer
    assert "$shortcut.WorkingDirectory = $ProjectRoot" in installer


def test_windows_shortcut_installer_creates_portable_link(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install_desktop_shortcut_windows.ps1"),
            "-DestinationDirectory",
            str(tmp_path),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    shortcut_path = tmp_path / "ArticleOps 创作与投递.lnk"
    assert shortcut_path.is_file()
    escaped_shortcut_path = str(shortcut_path).replace("'", "''")
    inspect_command = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{escaped_shortcut_path}'"
        ");"
        'Write-Output ($s.TargetPath+"|"+$s.Arguments+"|"+$s.WorkingDirectory)'
    )
    inspection = subprocess.run(
        [powershell, "-NoProfile", "-Command", inspect_command],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert inspection.returncode == 0, inspection.stdout + inspection.stderr
    target, arguments, working_directory = inspection.stdout.strip().split("|", 2)
    assert target.lower().endswith("powershell.exe")
    assert str(ROOT / "scripts" / "launch_articleops_windows.ps1") in arguments
    assert working_directory == str(ROOT)


def test_windows_launcher_reuses_healthy_current_installation(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    flask_port = _find_free_port()
    mcp_port = _find_free_port()
    while mcp_port == flask_port:
        mcp_port = _find_free_port()
    launcher = _write_launcher_fixture(tmp_path, flask_port, mcp_port)
    flask_process = _start_test_http_process(
        tmp_path / "run_flask_production.py",
        flask_port,
    )
    mcp_process = _start_test_http_process(
        tmp_path / "mcp_server.server.py",
        mcp_port,
    )
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-NoBrowser",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert flask_process.poll() is None
        assert mcp_process.poll() is None
    finally:
        for process in (flask_process, mcp_process):
            process.terminate()
            process.wait(timeout=5)


def test_windows_launcher_does_not_kill_foreign_port_owner(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is unavailable")

    flask_port = _find_free_port()
    mcp_port = _find_free_port()
    while mcp_port == flask_port:
        mcp_port = _find_free_port()
    launcher = _write_launcher_fixture(tmp_path, flask_port, mcp_port)
    foreign_process = _start_test_http_process(
        tmp_path / "foreign_server.py",
        flask_port,
    )
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-NoBrowser",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode != 0
        assert "未自动结束该进程" in result.stdout + result.stderr
        assert foreign_process.poll() is None
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)


def test_v046_release_docs_describe_runtime_only_test_behavior() -> None:
    docs = read("docs/deployment/PRODUCTION_WINDOWS.md")
    release = read("docs/releases/v0.4.3-weibo-mcp.md")

    assert "仍会执行 `compileall`" in docs
    assert "跳过不存在的 `pytest`" in docs
    assert "排除 `.git`、`data`、数据库、Cookie" in release
    assert "ArticleOps-upgrade-v0.4.6" in docs
    assert "data\\upgrade_backups" in docs
    assert "start_article_draft_delivery" in docs
