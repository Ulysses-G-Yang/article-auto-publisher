const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const sourcePath = process.argv[2];
const scenario = process.argv[3];
assert.ok(sourcePath, 'content-studio.js path is required');
assert.ok(scenario, 'scenario is required');

class FakeClassList {
  constructor(...names) { this.names = new Set(names); }
  add(...names) { names.forEach(name => this.names.add(name)); }
  remove(...names) { names.forEach(name => this.names.delete(name)); }
  contains(name) { return this.names.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) this.names.add(name); else this.names.delete(name);
    return enabled;
  }
}

class FakeElement {
  constructor(id = '', ...classes) {
    this.id = id;
    this.dataset = {};
    this.classList = new FakeClassList(...classes);
    this.attributes = new Map();
    this.children = [];
    this.textContent = '';
    this.disabled = false;
    this.scrollOptions = null;
    this.stageState = null;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  addEventListener() {}
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  querySelector(selector) {
    if (selector === '[data-stage-state]') return this.stageState;
    return null;
  }
  scrollIntoView(options) { this.scrollOptions = options; }
  focus() {}
  get childElementCount() { return this.children.length; }
}

const elements = new Map();
function add(id, ...classes) {
  const element = new FakeElement(id, ...classes);
  elements.set(id, element);
  return element;
}

const root = add('content-studio');
root.dataset.publicationAdviceUrlTemplate = '/api/content-drafts/{draft_id}/publication-advice';
root.dataset.draftUrlTemplate = '/api/content-drafts/{draft_id}';
add('publication-advice-panel', 'd-none');
add('generate-publication-advice');
elements.get('generate-publication-advice').disabled = false;
add('generate-publication-advice-label').textContent = '生成平台建议';
add('cancel-publication-advice', 'd-none');
add('publication-advice-countdown', 'd-none');
add('publication-advice-seconds').textContent = '90';
add('publication-advice-progress');
add('publication-advice-status');
add('publication-advice-error', 'd-none');
add('publication-advice-results');
add('draft-title');
add('save-indicator');
add('save-indicator-text');
add('save-draft-now');

const stages = new Map();
for (const id of ['saving', 'rules', 'request', 'validation']) {
  const item = new FakeElement();
  item.dataset.adviceStage = id;
  item.stageState = new FakeElement();
  item.stageState.textContent = '等待开始';
  stages.set(id, item);
}

let now = 0;
let timerSequence = 0;
const timers = [];
function schedule(kind, callback, delay) {
  const timer = {id: ++timerSequence, kind, callback, delay, active: true};
  timers.push(timer);
  return timer;
}
function cancelTimer(timer) { if (timer) timer.active = false; }
function activeTimers() { return timers.filter(timer => timer.active); }
async function fireTimer(timer) {
  assert.ok(timer && timer.active, 'timer must be active');
  if (timer.kind === 'timeout') timer.active = false;
  timer.callback();
  await flush();
}
async function flush(rounds = 12) {
  for (let index = 0; index < rounds; index += 1) await Promise.resolve();
}

class FakeDate extends Date {
  static now() { return now; }
}

const fetchCalls = [];
let currentAdviceRequest = null;
const adviceRequests = [];
let saveGateResolve = null;
let validationResolve = null;

function response(payload, payloadPromise = null) {
  return {
    ok: true,
    status: 200,
    json: () => payloadPromise || Promise.resolve(payload),
  };
}

function fetchMock(url, options = {}) {
  fetchCalls.push({url, options});
  if (options.method === 'PATCH') {
    return new Promise(resolve => { saveGateResolve = resolve; });
  }
  const payload = {
    draft_id: 'draft-1', revision: 7, model: 'test-model',
    recommendations: [{platform: 'zhihu', suggested_community: '科技',
      suggested_topics: ['显示器'], topic_queries: ['显示器'], keywords: ['屏幕'], reason: '匹配'}],
  };
  if (scenario === 'success') return Promise.resolve(response(payload));
  if (scenario === 'validation') {
    const payloadPromise = new Promise(resolve => { validationResolve = () => resolve(payload); });
    return Promise.resolve(response(payload, payloadPromise));
  }
  const pending = {};
  pending.promise = new Promise((resolve, reject) => {
    pending.resolve = () => resolve(response(payload));
    pending.reject = reject;
  });
  pending.signal = options.signal;
  currentAdviceRequest = pending;
  adviceRequests.push(pending);
  return pending.promise;
}

const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement() { return new FakeElement(); },
  querySelector(selector) {
    const match = selector.match(/^\[data-advice-stage="([^"]+)"\]$/);
    if (match) return stages.get(match[1]) || null;
    return null;
  },
  querySelectorAll() { return []; },
};
const window = {
  ArticleOpsUi: {},
  matchMedia() { return {matches: scenario === 'success'}; },
  setTimeout(callback, delay) { return schedule('timeout', callback, delay); },
  clearTimeout: cancelTimer,
  setInterval(callback, delay) { return schedule('interval', callback, delay); },
  clearInterval: cancelTimer,
};
const context = {
  AbortController,
  Date: FakeDate,
  URLSearchParams,
  console,
  document,
  window,
  fetch: fetchMock,
  setTimeout: window.setTimeout,
  clearTimeout: window.clearTimeout,
  setInterval: window.setInterval,
  clearInterval: window.clearInterval,
};
context.globalThis = context;

let source = fs.readFileSync(sourcePath, 'utf8');
const initMarker = '    init();';
assert.ok(source.includes(initMarker), 'content studio init marker missing');
source = source.replace(initMarker, `
    globalThis.__publicationAdviceTest = {
      state, generatePublicationAdvice, cancelPublicationAdvice,
    };`);
vm.runInNewContext(source, context, {filename: sourcePath});

const api = context.__publicationAdviceTest;
api.state.platforms = [{id: 'zhihu', display_name: '知乎', delivery_enabled: true}];
api.state.switcherToggles = {zhihu: true};
api.state.draft = {
  draft_id: 'draft-1', title: '测试文章', revision: 7, content_schema_version: 1,
  blocks: [{block_id: 'p-1', type: 'text', text: '正文', position: 0}],
  cover: {strategy: 'NONE', asset_id: null}, targets: [],
};

function stageSnapshot() {
  return Object.fromEntries([...stages].map(([id, item]) => [id, {
    text: item.stageState.textContent,
    active: item.classList.contains('is-active'),
    complete: item.classList.contains('is-complete'),
  }]));
}

async function main() {
  if (scenario === 'success') {
    const run = api.generatePublicationAdvice();
    await run;
    assert.equal(elements.get('generate-publication-advice-label').textContent, '生成平台建议');
    assert.equal(elements.get('generate-publication-advice').disabled, false);
    assert.equal(elements.get('cancel-publication-advice').classList.contains('d-none'), true);
    assert.equal(elements.get('publication-advice-countdown').classList.contains('d-none'), true);
    assert.equal(activeTimers().length, 0, 'success leaked advice timers');
    assert.deepEqual(stageSnapshot(), {
      saving: {text: '已完成', active: false, complete: true},
      rules: {text: '已完成', active: false, complete: true},
      request: {text: '已完成', active: false, complete: true},
      validation: {text: '已完成', active: false, complete: true},
    });
    assert.equal(elements.get('publication-advice-results').scrollOptions?.behavior, 'auto');
    assert.equal(elements.get('publication-advice-results').scrollOptions?.block, 'start');
  } else if (scenario === 'validation') {
    const run = api.generatePublicationAdvice();
    await flush(50);
    assert.ok(validationResolve, 'response JSON validation did not start');
    assert.equal(stages.get('validation').classList.contains('is-active'), true);
    assert.equal(elements.get('publication-advice-status').textContent, '正在校验返回结果');
    validationResolve();
    await run;
    assert.equal(activeTimers().length, 0, 'validation completion leaked timers');
  } else if (scenario === 'cancel-save') {
    let releaseSave;
    api.state.mutationPromise = new Promise(resolve => { releaseSave = resolve; });
    const run = api.generatePublicationAdvice();
    await flush();
    assert.equal(stages.get('saving').stageState.textContent, '正在保存当前文章');
    assert.equal(api.state.publicationAdviceLoading, true, 'loading was not set before save await');
    api.cancelPublicationAdvice();
    await run;
    releaseSave();
    await flush();
    assert.equal(fetchCalls.length, 0, 'cancel during save still called AI');
    assert.match(elements.get('publication-advice-status').textContent, /已取消生成平台建议/);
    assert.equal(activeTimers().length, 0, 'cancel leaked advice timers');
  } else if (scenario === 'timeout') {
    const run = api.generatePublicationAdvice();
    await flush();
    assert.equal(stages.get('request').stageState.textContent, '正在请求 AI');
    assert.ok(currentAdviceRequest.signal instanceof AbortSignal, 'AI request has no AbortSignal');
    assert.equal(elements.get('publication-advice-seconds').textContent, '90');
    const countdown = activeTimers().find(timer => timer.kind === 'interval' && timer.delay === 1000);
    now = 1000;
    await fireTimer(countdown);
    assert.equal(elements.get('publication-advice-seconds').textContent, '89');
    const timeout = activeTimers().find(timer => timer.kind === 'timeout' && timer.delay === 90000);
    assert.ok(timeout, 'missing exact 90 second timeout');
    now = 90000;
    await fireTimer(timeout);
    await run;
    assert.equal(currentAdviceRequest.signal.aborted, true);
    assert.match(elements.get('publication-advice-error').textContent, /生成平台建议已超时（90 秒）/);
    assert.equal(elements.get('generate-publication-advice').disabled, false);
    assert.equal(elements.get('generate-publication-advice-label').textContent, '生成平台建议');
    assert.equal(activeTimers().length, 0, 'timeout leaked advice timers');
  } else if (scenario === 'stale-request') {
    const first = api.generatePublicationAdvice();
    await flush();
    const oldRequest = currentAdviceRequest;
    api.cancelPublicationAdvice();
    await first;
    assert.match(elements.get('publication-advice-status').textContent, /已取消生成平台建议/);
    const second = api.generatePublicationAdvice();
    await flush();
    const newRequest = currentAdviceRequest;
    assert.notEqual(oldRequest, newRequest);
    oldRequest.resolve();
    await flush();
    assert.equal(api.state.publicationAdviceLoading, true, 'late old request stopped the new run');
    assert.equal(elements.get('generate-publication-advice').disabled, true);
    assert.equal(stages.get('request').classList.contains('is-active'), true);
    api.cancelPublicationAdvice();
    await second;
    assert.equal(activeTimers().length, 0, 'new run cancellation leaked timers');
  } else {
    throw new Error(`unknown scenario: ${scenario}`);
  }
  process.stdout.write(JSON.stringify({scenario, fetchCalls: fetchCalls.length}));
}

main().catch(error => {
  process.stderr.write(String(error.stack || error));
  process.exit(1);
});
