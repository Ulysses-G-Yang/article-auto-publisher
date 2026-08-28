import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_shared_ui_text_helpers_hide_internal_codes_and_preserve_chinese() -> None:
    source = (ROOT / "web/static/js/app.js").read_text(encoding="utf-8")
    program = f"""
const vm = require('node:vm');
const assert = require('node:assert/strict');

const classList = {{
  add() {{}}, remove() {{}}, contains() {{ return false; }},
  toggle() {{ return false; }},
}};
const document = {{
  readyState: 'complete',
  body: {{ classList, appendChild() {{}} }},
  getElementById() {{ return null; }},
  querySelectorAll() {{ return []; }},
  addEventListener() {{}},
}};
const window = {{
  coreui: {{}},
  innerWidth: 1440,
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  addEventListener() {{}},
}};
const context = {{ window, document, console }};
vm.runInNewContext({json.dumps(source)}, context);

const ui = window.ArticleOpsUi;
assert.equal(ui.statusLabel('DRAFT_SAVED_WITH_WARNINGS'), '草稿已保存（需核对）');
assert.equal(ui.statusLabel('PARTIAL_FAIL'), '部分成功 / 部分失败');
assert.equal(ui.statusLabel('UNREGISTERED_STATE'), '状态待确认');
assert.equal(ui.errorCodeLabel('LOGIN_REQUIRED'), '需要重新登录');
assert.equal(
  ui.userFacingErrorMessage('CONTENT_VALIDATION_ERROR', 'CONTENT_VALIDATION_ERROR: 正文为空'),
  '正文为空',
);
assert.equal(
  ui.userFacingErrorMessage('UNREGISTERED_ERROR', 'UNREGISTERED_ERROR'),
  '处理异常，请查看执行日志',
);
"""
    result = subprocess.run(
        ["node", "-e", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
