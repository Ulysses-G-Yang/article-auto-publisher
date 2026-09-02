const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const sourcePath = process.argv[2];
assert.ok(sourcePath, 'app.js path is required');

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
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.classList = new FakeClassList();
    this.attributes = new Map();
    this.listeners = new Map();
    this.focusCount = 0;
    this.groups = [];
    this.links = [];
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatch(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  focus() { this.focusCount += 1; }
  querySelector(selector) {
    if (selector === '.sidebar-group-toggle') return this.groupToggle || null;
    if (selector === '.sidebar-group-panel') return this.groupPanel || null;
    if (selector === '.nav-link.active') return this.activeLink || null;
    return null;
  }
  querySelectorAll(selector) {
    if (selector === '[data-sidebar-group]') return this.groups;
    return [];
  }
  hasPointerCapture() { return false; }
  setPointerCapture() {}
  releasePointerCapture() {}
}

function makeGroup(id, active = false) {
  const group = new FakeElement();
  group.dataset.sidebarGroup = id;
  group.groupToggle = new FakeElement();
  group.groupPanel = new FakeElement();
  group.groupPanel.activeLink = active ? new FakeElement() : null;
  return group;
}

const storage = new Map([
  ['articleops.sidebar-groups.v2', JSON.stringify({ workspace: false, operations: false })],
  ['articleops.sidebar-width.v2', '999'],
  ['articleops.theme.v2', 'dark'],
]);
let fetchCalls = 0;

const rootStyle = new Map();
const documentElement = {
  dataset: {},
  style: {
    setProperty(name, value) { rootStyle.set(name, value); },
    getPropertyValue(name) { return rootStyle.get(name) || ''; },
  },
};
const body = new FakeElement('body');
body.appendChild = () => {};
const sidebar = new FakeElement('app-sidebar');
const workspaceGroup = makeGroup('workspace');
const operationsGroup = makeGroup('operations', true);
sidebar.groups = [workspaceGroup, operationsGroup];
sidebar.links = [new FakeElement()];

const elements = new Map();
for (const id of [
  'sidebar-toggle', 'sidebar-close', 'sidebar-backdrop', 'sidebar-resizer',
  'theme-toggle', 'theme-icon',
]) elements.set(id, new FakeElement(id));
elements.set('app-sidebar', sidebar);

const documentListeners = new Map();
const document = {
  readyState: 'complete',
  body,
  documentElement,
  getElementById(id) { return elements.get(id) || null; },
  querySelectorAll(selector) {
    if (selector === '#app-sidebar a') return sidebar.links;
    return [];
  },
  addEventListener(type, listener) {
    if (!documentListeners.has(type)) documentListeners.set(type, []);
    documentListeners.get(type).push(listener);
  },
  createElement() { return new FakeElement(); },
};
const windowListeners = new Map();
const window = {
  coreui: {},
  innerWidth: 1440,
  localStorage: {
    getItem(key) { return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, String(value)); },
  },
  addEventListener(type, listener) {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(listener);
  },
};
const context = {
  window,
  document,
  console,
  fetch() { fetchCalls += 1; throw new Error('shell initialization must not call APIs'); },
  getComputedStyle(element) {
    return { getPropertyValue: name => element?.style?.getPropertyValue(name) || '' };
  },
};

vm.runInNewContext(fs.readFileSync(sourcePath, 'utf8'), context, { filename: sourcePath });

// Saved groups stay collapsed unless they contain the active route.
assert.equal(workspaceGroup.classList.contains('is-collapsed'), true);
assert.equal(workspaceGroup.groupToggle.getAttribute('aria-expanded'), 'false');
assert.equal(operationsGroup.classList.contains('is-collapsed'), false);
assert.equal(operationsGroup.groupToggle.getAttribute('aria-expanded'), 'true');
workspaceGroup.groupToggle.dispatch('click');
assert.equal(workspaceGroup.classList.contains('is-collapsed'), false);
assert.equal(JSON.parse(storage.get('articleops.sidebar-groups.v2')).workspace, true);

// Invalid persisted widths clamp to the supported range and keyboard changes persist.
const resizer = elements.get('sidebar-resizer');
assert.equal(rootStyle.get('--ao-sidebar-width-current'), '320px');
assert.equal(resizer.getAttribute('aria-valuenow'), '320');
let prevented = false;
resizer.dispatch('keydown', { key: 'Home', preventDefault() { prevented = true; } });
assert.equal(prevented, true);
assert.equal(rootStyle.get('--ao-sidebar-width-current'), '216px');
assert.equal(storage.get('articleops.sidebar-width.v2'), '216');
resizer.dispatch('keydown', { key: 'End', preventDefault() {} });
assert.equal(rootStyle.get('--ao-sidebar-width-current'), '320px');
resizer.dispatch('keydown', { key: 'ArrowLeft', preventDefault() {} });
assert.equal(rootStyle.get('--ao-sidebar-width-current'), '312px');
assert.equal(storage.get('articleops.sidebar-width.v2'), '312');

// Theme applies before interaction, then toggles and persists without a request.
assert.equal(documentElement.dataset.theme, 'dark');
assert.equal(elements.get('theme-toggle').getAttribute('aria-label'), '切换为浅色模式');
elements.get('theme-toggle').dispatch('click');
assert.equal(documentElement.dataset.theme, 'light');
assert.equal(storage.get('articleops.theme.v2'), 'light');

// Desktop collapse persists. Mobile Escape closes the drawer and restores toggle focus.
elements.get('sidebar-toggle').dispatch('click');
assert.equal(body.classList.contains('sidebar-collapsed'), true);
assert.equal(storage.get('articleops.sidebar-collapsed.v1'), 'true');
window.innerWidth = 390;
elements.get('sidebar-toggle').dispatch('click');
assert.equal(body.classList.contains('sidebar-mobile-open'), true);
for (const listener of documentListeners.get('keydown') || []) listener({ key: 'Escape' });
assert.equal(body.classList.contains('sidebar-mobile-open'), false);
assert.equal(elements.get('sidebar-toggle').focusCount, 1);
assert.equal(fetchCalls, 0);

// Syntactically valid JSON is not necessarily a valid preference object.
// Corrupted or manually edited storage must never abort shell initialization.
for (const rawValue of ['null', '[]', '7', '"workspace"']) {
  storage.set('articleops.sidebar-groups.v2', rawValue);
  const preference = context.readJsonPreference('articleops.sidebar-groups.v2');
  assert.equal(preference !== null && typeof preference === 'object', true);
  assert.equal(Array.isArray(preference), false);
  assert.equal(Object.keys(preference).length, 0);
}
