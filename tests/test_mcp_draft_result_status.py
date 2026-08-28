# ruff: noqa: E501
import json
import os
import subprocess
import tempfile
from pathlib import Path

from mcp_server.tools import _mcp_status_for_plan, _safe_plan_target

ROOT = Path(__file__).resolve().parents[1]


def _target(**overrides):
    value = {
        "platform": "weibo",
        "account_display_name": "安全账号",
        "status": "DRAFT_SAVED",
        "operation_id": "operation-1",
        "article_mapping_status": None,
        "error_code": None,
        "error_message": None,
    }
    value.update(overrides)
    return value


def test_normal_draft_result_keeps_existing_mcp_completion_message() -> None:
    status, message = _mcp_status_for_plan(
        {
            "overall_status": "SUCCESS",
            "targets": [_target()],
        }
    )

    assert status == "completed"
    assert message == "所有平台草稿均已保存并通过平台侧验证。"


def test_degraded_and_warning_results_complete_but_require_review() -> None:
    for target in (
        _target(degraded="draft_list_confirmed"),
        _target(
            status="DRAFT_SAVED_WITH_WARNINGS",
            error_message="正文或图片完整性待核对",
        ),
    ):
        status, message = _mcp_status_for_plan(
            {"overall_status": "SUCCESS", "targets": [target]}
        )
        assert status == "completed"
        assert "核对" in message
        assert "通过平台侧验证" not in message


def test_complete_reopen_evidence_keeps_normal_success_semantics() -> None:
    status, message = _mcp_status_for_plan(
        {
            "overall_status": "SUCCESS",
            "targets": [
                _target(
                    verification_evidence={
                        "reopen_title_match": True,
                        "reopen_dom_blocks_match": True,
                        "draft_entity_bound": False,
                    }
                )
            ],
        }
    )

    assert status == "completed"
    assert "通过平台侧验证" in message


def test_incomplete_is_terminal_and_not_reported_as_running() -> None:
    status, message = _mcp_status_for_plan(
        {
            "overall_status": "FATAL",
            "targets": [_target(status="DELIVERY_INCOMPLETE")],
        }
    )

    assert status == "failed"
    assert "投递未完成" in message


def test_plan_target_evidence_is_whitelisted_and_redacted() -> None:
    target = _safe_plan_target(
        _target(
            degraded="draft_list_confirmed",
            verification_evidence={
                "save_response_2xx": True,
                "draft_list_title_unique": True,
                "draft_entity_bound": True,
                "draft_entity_source": "save_response_id",
                "draft_entity_id_match": True,
                "summary": "profile=fixture-profile; token=fixture-token",
                "draft_url": "https://example.invalid/draft/1?token=fixture-token",
                "profile_path": "D:/fixture/profile",
                "access_token": "fixture-token",
                "nested": {"cookie": "fixture-cookie"},
            },
        )
    )

    evidence = target["verification_evidence"]
    assert target["degraded"] == "draft_list_confirmed"
    assert evidence["draft_entity_bound"] is True
    assert evidence["draft_entity_source"] == "save_response_id"
    assert evidence["draft_url"] is None
    assert "profile_path" not in evidence
    assert "access_token" not in evidence
    assert "nested" not in evidence
    assert "fixture-profile" not in evidence["summary"]
    assert "fixture-token" not in evidence["summary"]


def test_frontend_consumes_full_operation_and_keeps_safe_draft_fallback() -> None:
    index = (ROOT / "web/templates/index.html").read_text(encoding="utf-8")
    studio = (ROOT / "web/static/js/content-studio.js").read_text(encoding="utf-8")

    assert "deliveryClass(op.status, op)" in index
    assert "deliveryLabel(op.status, op)" in index
    assert "deliveryDetail(op)" in index
    assert "deliveryDraftHref(op)" in index
    assert "this.safeDraftUrl(operation.draft_url) || this.platformDraftBoxUrl(operation.platform)" in index
    assert "target?.degraded && target.status === 'DRAFT_SAVED'" in studio
    assert "planDisplayStatus(state.plan)" in studio
    assert "target.status === 'DELIVERY_INCOMPLETE'" in studio
    assert "verificationControls.set(target, { button: verify, result: verifyResult })" in studio
    assert "verifyDraft(target)" in studio
    assert "state.verifyingOperations.has(opId)" in studio
    assert 'setAttribute(\'aria-live\', \'polite\')' in studio


def test_content_studio_runtime_keeps_row_local_verify_and_status_labels() -> None:
    """Run the real studio helpers with a tiny DOM shim; no browser dependency."""

    source = (ROOT / "web/static/js/content-studio.js").read_text(encoding="utf-8")
    marker = "    init();\n})();"
    assert marker in source
    source = source.replace(
        marker,
        "    globalThis.__articleOpsHooks = { state, renderPlan, verifyDraft, targetStatusLabel };\n})();",
        1,
    )
    node_program = f"""
import * as vm from 'node:vm';
class FakeClassList {{
  constructor(owner) {{ this.owner = owner; }}
  add(...names) {{ for (const name of names) this.owner.className += ` ${{name}}`; }}
  remove(...names) {{ this.owner.className = this.owner.className.split(/\\s+/).filter(item => item && !names.includes(item)).join(' '); }}
  toggle(name, force) {{
    const has = this.owner.className.split(/\\s+/).includes(name);
    const next = force === undefined ? !has : Boolean(force);
    if (next && !has) this.add(name);
    if (!next && has) this.remove(name);
    return next;
  }}
}}
class FakeElement {{
  constructor(tag = 'div', id = '') {{
    this.tagName = tag.toUpperCase(); this.id = id; this.children = [];
    this.parentNode = null; this.className = ''; this.textContent = '';
    this.attributes = {{}}; this.dataset = {{}}; this.listeners = {{}};
    this.classList = new FakeClassList(this); this.disabled = false;
  }}
  append(...nodes) {{ for (const node of nodes) {{ if (!node) continue; node.parentNode = this; this.children.push(node); }} }}
  appendChild(node) {{ this.append(node); return node; }}
  replaceChildren(...nodes) {{ this.children = []; this.append(...nodes); }}
  addEventListener(type, handler) {{ this.listeners[type] = handler; }}
  click() {{ return this.listeners.click ? this.listeners.click({{ currentTarget: this }}) : undefined; }}
  setAttribute(name, value) {{ this.attributes[name] = String(value); }}
  getAttribute(name) {{ return this.attributes[name] ?? null; }}
  removeAttribute(name) {{ delete this.attributes[name]; }}
  closest(selector) {{
    let node = this;
    while (node) {{ if (selector === '.plan-target' && node.className.split(/\\s+/).includes('plan-target')) return node; node = node.parentNode; }}
    return null;
  }}
  querySelectorAll(selector) {{
    const found = [];
    const matches = node => selector === 'button' ? node.tagName === 'BUTTON' : selector === '[role="status"]' ? node.getAttribute('role') === 'status' : false;
    const visit = node => {{ for (const child of node.children) {{ if (matches(child)) found.push(child); visit(child); }} }};
    visit(this); return found;
  }}
}}
const elements = new Map();
const document = {{
  getElementById(id) {{ if (!elements.has(id)) elements.set(id, new FakeElement('div', id)); return elements.get(id); }},
  createElement(tag) {{ return new FakeElement(tag); }},
  querySelector() {{ return null; }},
  querySelectorAll() {{ return []; }},
}};
document.getElementById('content-studio').dataset = {{}};
const context = {{
  document,
  window: {{ matchMedia: () => ({{ matches: false }}), addEventListener() {{}}, location: {{ href: 'https://articleops.invalid/upload' }} }},
  console, setTimeout, clearTimeout, URL, URLSearchParams,
  fetch: () => Promise.reject(new Error('fetch not configured')),
}};
vm.runInNewContext({json.dumps(source)}, context);
const hooks = context.__articleOpsHooks;
const assert = (condition, message) => {{ if (!condition) throw new Error(message); }};
assert(hooks.targetStatusLabel({{ status: 'DRAFT_SAVED' }}) === '平台草稿已保存', 'normal label');
assert(hooks.targetStatusLabel({{ status: 'DRAFT_SAVED', degraded: 'draft_list_confirmed' }}) === '草稿已保存（完整性待核对）', 'degraded label');
assert(hooks.targetStatusLabel({{ status: 'DRAFT_SAVED_WITH_WARNINGS' }}) === '草稿已保存（需核对）', 'warning label');

hooks.state.platforms = [];
hooks.state.plan = {{ plan_id: 'plan-verify', status: 'FATAL', targets: [
  {{ platform: 'weibo', account_display_name: '甲', status: 'FAILED', operation_id: 'operation-a', mode: 'DRAFT' }},
  {{ platform: 'weibo', account_display_name: '乙', status: 'FAILED', operation_id: 'operation-b', mode: 'DRAFT' }},
]}};
hooks.renderPlan();
const rows = document.getElementById('plan-targets').children;
const buttons = rows.map(row => row.querySelectorAll('button')[0]);
assert(buttons.length === 2, 'two row-local verify buttons');
const calls = []; const pending = [];
context.fetch = url => {{ calls.push(String(url)); return new Promise(resolve => pending.push(resolve)); }};
const first = buttons[0].click(); const second = buttons[1].click();
await Promise.resolve();
assert(buttons[0].disabled && buttons[1].disabled, 'both buttons become busy');
assert(buttons[0].getAttribute('aria-busy') === 'true' && buttons[1].getAttribute('aria-busy') === 'true', 'busy state is per row');
pending[0]({{ ok: true, json: async () => ({{ title_matched: true }}) }});
pending[1]({{ ok: true, json: async () => ({{ title_matched: true }}) }});
await Promise.all([first, second]);
assert(calls.some(url => url.includes('operation-a/verify-draft')), 'first operation is verified');
assert(calls.some(url => url.includes('operation-b/verify-draft')), 'second operation is verified');
const results = rows.map(row => row.querySelectorAll('[role="status"]')[0]);
assert(results.every(result => result.textContent.includes('标题唯一匹配')), 'each row gets its own result');
assert(buttons.every(button => !button.disabled && button.getAttribute('aria-busy') === 'false'), 'busy state clears');

hooks.state.plan = {{ plan_id: 'plan-incomplete', status: 'FATAL', targets: [
  {{ platform: 'weibo', status: 'DELIVERY_INCOMPLETE', operation_id: 'operation-c', mode: 'DRAFT' }},
  {{ platform: 'zhihu', status: 'DELIVERY_INCOMPLETE', operation_id: 'operation-d', mode: 'DRAFT' }},
]}};
hooks.renderPlan();
assert(document.getElementById('plan-status').textContent === '投递未完成', 'all incomplete plan label');
"""
    descriptor, script_path = tempfile.mkstemp(suffix=".mjs")
    os.close(descriptor)
    script_file = Path(script_path)
    script_file.write_text(node_program, encoding="utf-8")
    try:
        result = subprocess.run(
            ["node", str(script_file)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script_file.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr
