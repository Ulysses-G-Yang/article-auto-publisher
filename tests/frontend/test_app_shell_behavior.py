import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_app_shell_preferences_and_mobile_focus_behavior() -> None:
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/frontend/app_shell_behavior.test.cjs"),
            str(ROOT / "web/static/js/app.js"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
