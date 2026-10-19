const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/themes.js', 'utf8');
const key = 'piaoyu-appearance-v1';

function page(saved, blocked = false) {
  const store = new Map(saved ? [[key, saved]] : []), events = {}, windowEvents = {}, label = {}, attrs = {};
  let ready = false;
  const button = {querySelector: () => label, setAttribute: (name, value) => {attrs[name] = value;}};
  const document = {
    documentElement: {dataset: {}},
    getElementById: () => ready ? button : null,
    addEventListener: (type, fn) => {events[type] = fn;}
  };
  const run = () => {
    vm.runInNewContext(source, {
      document,
      localStorage: {
        getItem: k => {if(blocked)throw Error('blocked');return store.get(k);},
        setItem: (k, v) => {if(blocked)throw Error('blocked');store.set(k, v);}
      },
      window: {addEventListener: (type, fn) => {windowEvents[type] = fn;}}
    });
    ready = true; events.DOMContentLoaded();
  };
  run();
  return {document, store, label, attrs, button,
    click: () => events.click({target: {closest: selector => selector === '#theme-switch' ? button : null}}),
    storage: event => windowEvents.storage(event)};
}

const normal = page();
assert.equal(normal.document.documentElement.dataset.theme, 'classic');
for (const [id, name] of [['paper','暖纸墨绿'],['graphite','石墨深色'],['swiss','黑白橙色'],['classic','经典蓝白']]) {
  normal.click();
  assert.equal(normal.document.documentElement.dataset.theme, id);
  assert.equal(normal.label.textContent, name);
  assert.equal(normal.store.get(key), id);
  assert.ok(normal.attrs['aria-label'].includes(name));
}
const restored = page('graphite');
assert.equal(restored.document.documentElement.dataset.theme, 'graphite');
restored.storage({key, newValue: 'swiss'});
assert.equal(restored.document.documentElement.dataset.theme, 'swiss');
restored.storage({key: 'unrelated', newValue: 'paper'});
assert.equal(restored.document.documentElement.dataset.theme, 'swiss');
restored.storage({key: null, newValue: null});
assert.equal(restored.document.documentElement.dataset.theme, 'classic');
assert.equal(page('unexpected').document.documentElement.dataset.theme, 'classic');
const blocked = page(null, true);
blocked.click();
assert.equal(blocked.document.documentElement.dataset.theme, 'paper');
console.log('PASS: theme cycle, persistence restore, tab sync and restricted storage fallback.');
