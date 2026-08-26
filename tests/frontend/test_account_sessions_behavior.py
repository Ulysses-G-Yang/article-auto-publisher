from __future__ import annotations
# ruff: noqa: E501, I001

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


NODE_HARNESS = r"""
const fs = require('fs');
const assert = require('node:assert/strict');
const vm = require('vm');

const scriptPath = process.argv[1];
const scenario = process.argv[2];
const calls = [];
const timers = [];
globalThis.__resolveLogin = null;

class ClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach(name => this.values.add(name)); }
  remove(...names) { names.forEach(name => this.values.delete(name)); }
  toggle(name, force) {
    const next = force === undefined ? !this.values.has(name) : Boolean(force);
    if (next) this.values.add(name); else this.values.delete(name);
    return next;
  }
}

class Element {
  constructor(tagName = 'div', id = '') {
    this.tagName = tagName.toUpperCase(); this.id = id; this.dataset = {};
    this.children = []; this.listeners = {}; this.classList = new ClassList();
    this.textContent = ''; this.disabled = false; this.checked = false;
    this.hidden = false; this.value = ''; this.type = ''; this.name = '';
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  dispatch(type, event = {}) {
    if (this.disabled && type === 'click') return undefined;
    return (this.listeners[type] || []).map(listener => listener({target: this, ...event}))[0];
  }
  click() { const result = this.dispatch('click'); if (this.type === 'radio') this.dispatch('change'); return result; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute() {}
}

const ids = [
  'multi-account-sessions', 'session-platforms', 'session-platforms-loading',
  'session-platforms-error', 'session-accounts-placeholder', 'session-accounts-loading',
  'session-accounts-error', 'show-archived-accounts', 'archived-account-count',
  'session-accounts-empty', 'session-account-list', 'session-login-status',
  'add-platform-account', 'refresh-account-sessions', 'account-activity-drawer',
  'account-activity-account', 'account-activity-list', 'account-activity-loading',
  'account-activity-empty', 'account-activity-error',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element('div', id)]));
elements['multi-account-sessions'].dataset = {
  platformsUrl: '/api/platforms', accountsUrlTemplate: '/api/platforms/{platform}/accounts',
  verifyUrlTemplate: '/api/accounts/{account_id}/verify',
  accountLoginUrlTemplate: '/api/account-sessions/{account_id}/login',
  loginUrlTemplate: '/api/platforms/{platform}/accounts/login',
  policyUrlTemplate: '/api/accounts/{account_id}/session-policy',
  logoutUrlTemplate: '/api/account-sessions/{account_id}/logout',
  archiveAccountUrlTemplate: '/api/account-sessions/{account_id}/archive',
  restoreAccountUrlTemplate: '/api/account-sessions/{account_id}/restore',
  clearLoginStateUrlTemplate: '/api/account-sessions/{account_id}/clear-login-state',
  deleteAccountUrlTemplate: '/api/account-sessions/{account_id}',
  activityUrlTemplate: '/api/account-sessions/{account_id}/activity',
};
function response(payload, ok = true, status = ok ? 200 : 409) { return {ok, status, json: async () => payload}; }
function account(id, status, fields = {}) {
  return {account_id: id, display_name: id, masked_platform_user_id: null, status: 'ACTIVE',
    session_status: status, persist_login: true, last_verified_at: null, ...fields};
}
const active = account('smzdm-1', 'VERIFYING', {login_in_progress: true, platform_login_in_progress: true});
const terminal = account('smzdm-1', 'VERIFYING', {login_in_progress: false, platform_login_in_progress: false, login_error_code: 'RATE_LIMITED'});
const idle = account('smzdm-2', 'VALID', {login_in_progress: false, platform_login_in_progress: true});
let accountResponses;
if (scenario === 'refresh-active') accountResponses = [[active, idle], [active, idle], [terminal, idle]];
else if (scenario === 'old-metadata') {
  accountResponses = [account('smzdm-1', 'UNVERIFIED')];
  for (let i = 0; i < 12; i += 1) accountResponses.push(account('smzdm-1', 'VERIFYING'));
} else if (scenario === 'conflict-add') accountResponses = [[], [active], [terminal]];
else if (scenario === 'conflict-verify' || scenario === 'conflict-login') {
  accountResponses = [[account('smzdm-1', 'UNVERIFIED')], [active], [terminal]];
} else if (scenario === 'failed-restart') accountResponses = [[], []];
else if (scenario === 'switch-add') accountResponses = [[]];
else if (scenario === 'switch-verify' || scenario === 'switch-login') {
  accountResponses = [[account('smzdm-1', 'UNVERIFIED')]];
} else accountResponses = [[]];
let xiaoheiheAccountResponses = scenario === 'other-platform'
  ? [[], [account('xhh-1', 'VALID')]]
  : [[]];
let smzdmAddAttempts = 0;

async function fetchMock(url, options = {}) {
  calls.push({url, method: options.method || 'GET'});
  if (url === '/api/platforms') return response({platforms: [
    {id: 'smzdm', display_name: 'SMZDM', account_enabled: true, delivery_enabled: false, sort_order: 1},
    {id: 'xiaoheihe', display_name: 'XHH', account_enabled: true, delivery_enabled: false, sort_order: 2},
  ]});
  if (url === '/api/platforms/smzdm/accounts/login') {
    if (scenario === 'switch-add') return new Promise(resolve => {
      globalThis.__resolveLogin = () => resolve(response({account_id: 'old'}, true, 202));
    });
    if (scenario === 'conflict-add') return response({error: 'LOGIN_IN_PROGRESS', message: 'busy'}, false, 409);
    if (scenario === 'failed-restart') {
      smzdmAddAttempts += 1;
      if (smzdmAddAttempts === 1) return response({error: 'LOGIN_REQUIRED', message: 'failed'}, false, 500);
    }
    return response({account_id: 'smzdm-new'}, true, 202);
  }
  if (url.startsWith('/api/accounts/') && url.endsWith('/verify')) {
    if (scenario === 'switch-verify') return new Promise(resolve => {
      globalThis.__resolveLogin = () => resolve(response({account_id: 'smzdm-1'}, true, 202));
    });
    if (scenario === 'conflict-verify') return response({error: 'LOGIN_IN_PROGRESS', message: 'busy'}, false, 409);
    return response({account_id: 'smzdm-1', session_status: 'VERIFYING'}, true, 202);
  }
  if (url.startsWith('/api/account-sessions/') && url.endsWith('/login')) {
    if (scenario === 'switch-login') return new Promise(resolve => {
      globalThis.__resolveLogin = () => resolve(response({account_id: 'smzdm-1'}, true, 202));
    });
    if (scenario === 'conflict-login') return response({error: 'LOGIN_IN_PROGRESS', message: 'busy'}, false, 409);
    return response({account_id: 'smzdm-1', session_status: 'VERIFYING'}, true, 202);
  }
  if (url.startsWith('/api/platforms/smzdm/accounts')) {
    const payload = accountResponses.length ? accountResponses.shift() : [];
    return response({platform: 'smzdm', accounts: Array.isArray(payload) ? payload : [payload]});
  }
  if (url === '/api/platforms/xiaoheihe/accounts/login') return response({account_id: 'xhh-new'}, true, 202);
  if (url.startsWith('/api/platforms/xiaoheihe/accounts')) {
    const payload = xiaoheiheAccountResponses.length ? xiaoheiheAccountResponses.shift() : [];
    return response({platform: 'xiaoheihe', accounts: Array.isArray(payload) ? payload : [payload]});
  }
  return response({});
}

globalThis.document = {getElementById: id => elements[id] || null, createElement: tag => new Element(tag)};
globalThis.window = {location: {search: ''}, confirm: () => true};
globalThis.bootstrap = {Offcanvas: {getOrCreateInstance: () => ({show() {}})}};
globalThis.fetch = fetchMock;
globalThis.setTimeout = fn => { const timer = {fn, canceled: false}; timers.push(timer); return timer; };
globalThis.clearTimeout = timer => { if (timer) timer.canceled = true; };
async function tick() { for (let i = 0; i < 8; i += 1) await Promise.resolve(); }
async function flushTimer() {
  const timer = timers.find(item => !item.canceled); if (!timer) return false;
  timer.canceled = true; await timer.fn(); await tick(); return true;
}
function buttons(root) {
  const found = []; const visit = node => { if (!node || typeof node !== 'object') return;
    if (node.tagName === 'BUTTON') found.push(node); for (const child of node.children || []) visit(child); };
  visit(root); return found;
}
function buttonByText(text) { return buttons(elements['session-account-list']).find(item => item.textContent === text); }
(async () => {
  vm.runInThisContext(fs.readFileSync(scriptPath, 'utf8'), {filename: scriptPath});
  await tick();
  const platforms = elements['session-platforms'];
  const smzdmInput = platforms.children.find(node => node.tagName === 'INPUT' && node.value === 'smzdm');
  const xiaoheiheInput = platforms.children.find(node => node.tagName === 'INPUT' && node.value === 'xiaoheihe');
  assert(smzdmInput, 'SMZDM input missing'); assert(xiaoheiheInput, 'XHH input missing');
  (scenario === 'other-platform' ? xiaoheiheInput : smzdmInput).click(); await tick();
  if (scenario === 'refresh-active') {
    assert(elements['add-platform-account'].disabled, 'active task did not disable add');
    assert(timers.filter(item => !item.canceled).length === 1, 'active task did not create one timer');
    elements['refresh-account-sessions'].click(); await tick();
    assert(timers.filter(item => !item.canceled).length === 1, 'refresh duplicated poller');
    assert(await flushTimer(), 'active poll did not run');
    assert(!elements['add-platform-account'].disabled, 'terminal false did not release add');
    assert(timers.filter(item => !item.canceled).length === 0, 'terminal poller remained');
    assert(elements['session-accounts-error'].textContent.length > 0, 'terminal error was not shown');
  } else if (scenario === 'old-metadata') {
    const verify = (elements['session-account-list'].children[0]?.children[2]?.children || [])[0];
    assert(verify, 'old service verify button missing'); const pending = verify.click(); await tick(); await pending;
    for (let i = 0; i < 12; i += 1) assert(await flushTimer(), 'missing bounded timer ' + i);
    assert(!elements['add-platform-account'].disabled, 'old service poll did not release add');
    assert(timers.filter(item => !item.canceled).length === 0, 'old service poll was unbounded');
    assert(elements['session-login-status'].textContent.length > 0, 'bounded stop was not reported');
  } else if (scenario === 'conflict-add' || scenario === 'conflict-verify' || scenario === 'conflict-login') {
    let pending;
    if (scenario === 'conflict-add') pending = elements['add-platform-account'].click();
    else if (scenario === 'conflict-verify') pending = buttonByText('验证现有登录态')?.click();
    else pending = buttonByText('重新登录')?.click();
    assert(pending, 'conflict entry button missing'); await tick(); await pending;
    const endpoint = scenario === 'conflict-add'
      ? '/api/platforms/smzdm/accounts/login'
      : scenario === 'conflict-verify' ? '/api/accounts/smzdm-1/verify' : '/api/account-sessions/smzdm-1/login';
    assert(calls.filter(item => item.url === endpoint && item.method === 'POST').length === 1, '409 caused duplicate POST');
    assert(elements['add-platform-account'].disabled, '409 recovery did not follow active task');
    assert(timers.filter(item => !item.canceled).length === 1, '409 recovery did not create one poller');
    assert(await flushTimer(), '409 recovery poll did not run');
    assert(!elements['add-platform-account'].disabled, '409 terminal state did not release add');
    assert(timers.filter(item => !item.canceled).length === 0, '409 recovery left duplicate poller');
  } else if (scenario === 'failed-restart') {
    const add = elements['add-platform-account'];
    const first = add.click(); await tick(); await first;
    assert(!add.disabled, 'failed login did not unlock add');
    assert(calls.filter(item => item.url === '/api/platforms/smzdm/accounts/login').length === 1, 'failed login POST duplicated');
    const second = add.click(); await tick(); await second;
    assert(calls.filter(item => item.url === '/api/platforms/smzdm/accounts/login').length === 2, 'retry did not issue exactly one POST');
    assert(add.disabled, 'successful retry did not track inflight login');
    assert(await flushTimer(), 'successful retry poll did not run');
    assert(timers.filter(item => !item.canceled).length === 0, 'retry left a poller');
  } else if (scenario === 'other-platform') {
    const add = elements['add-platform-account'];
    const pending = add.click(); await tick(); await pending;
    assert(calls.filter(item => item.url === '/api/platforms/xiaoheihe/accounts/login').length === 1, 'other platform POST missing');
    assert(!add.disabled, 'other platform inherited SMZDM lock');
    assert(await flushTimer(), 'other platform poll did not run');
    assert(!add.disabled, 'other platform terminal state disabled add');
    assert(elements['session-login-status'].textContent === '账号状态已更新。', 'other platform status was not preserved');
  } else if (scenario === 'double-click') {
    const add = elements['add-platform-account']; const first = add.click(); const second = add.click();
    await tick(); await first; await second;
    assert(calls.filter(item => item.url.endsWith('/accounts/login')).length === 1, 'duplicate login POST');
    assert(add.disabled, 'inflight add click was not blocked');
    assert(await flushTimer(), 'inflight login poll did not run');
  } else if (scenario === 'switch-add' || scenario === 'switch-verify' || scenario === 'switch-login') {
    const add = elements['add-platform-account'];
    let pending;
    if (scenario === 'switch-add') pending = add.click();
    else if (scenario === 'switch-verify') {
      const verify = buttonByText('验证现有登录态'); assert(verify, 'verify switch button missing');
      pending = verify.click();
    } else {
      const login = buttonByText('重新登录'); assert(login, 'login switch button missing');
      pending = login.click();
    }
    await tick();
    assert(globalThis.__resolveLogin, 'login POST was not pending');
    xiaoheiheInput.click(); await tick(); globalThis.__resolveLogin(); await pending;
    assert(elements['session-login-status'].textContent === '', 'stale response wrote new platform status');
    assert(timers.filter(item => !item.canceled).length === 0, 'stale response revived poller');
    assert(!add.disabled, 'platform switch left add disabled');
  }
  process.stdout.write(JSON.stringify({scenario, timers: timers.filter(item => !item.canceled).length}));
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exit(1); });
"""


def run_node_scenario(scenario: str) -> dict:
    result = subprocess.run(
        ["node", "-e", NODE_HARNESS, str(ROOT / "web/static/js/account-sessions.js"), scenario],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_refresh_recovers_one_active_smzdm_poller_and_terminal_state() -> None:
    assert run_node_scenario("refresh-active")["timers"] == 0


def test_old_service_metadata_uses_bounded_compatibility_polling() -> None:
    assert run_node_scenario("old-metadata")["timers"] == 0


def test_smzdm_409_recovery_tracks_the_existing_active_login() -> None:
    for scenario in ("conflict-add", "conflict-verify", "conflict-login"):
        assert run_node_scenario(scenario)["timers"] == 0


def test_failed_login_can_be_restarted_without_duplicate_post() -> None:
    assert run_node_scenario("failed-restart")["timers"] == 0


def test_other_platform_login_does_not_inherit_smzdm_lock() -> None:
    assert run_node_scenario("other-platform")["timers"] == 0


def test_smzdm_inflight_click_and_platform_switch_are_isolated() -> None:
    assert run_node_scenario("double-click")["timers"] == 0
    for scenario in ("switch-add", "switch-verify", "switch-login"):
        assert run_node_scenario(scenario)["timers"] == 0
