from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "scenario",
    ("success", "validation", "cancel-save", "timeout", "stale-request"),
)
def test_publication_advice_run_is_bounded_and_cancellable(scenario: str) -> None:
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/frontend/publication_advice_behavior.test.cjs"),
            str(ROOT / "web/static/js/content-studio.js"),
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout)["scenario"] == scenario
