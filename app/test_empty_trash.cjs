const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'web', 'app.js'), 'utf8');
const blocks = [
  source.slice(source.indexOf('function updateTrashControls('), source.indexOf('async function deleteDuplicates(')),
  source.slice(source.indexOf('async function emptyTrash('), source.indexOf('async function deleteSelectedInvoices(')),
  source.slice(source.indexOf('function confirmInvoiceDeletion('), source.indexOf('async function exportSelectedZip('))
].join('\n');

function fixture() {
  const elements = Object.fromEntries([
    '#empty-trash-button', '#trash-list', '#delete-confirm-title',
    '#delete-confirm-message', '#delete-confirm-note', '#delete-confirm-submit'
  ].map(id => [id, {disabled: false, textContent: '', innerHTML: ''}]));
  const restores = [{disabled: false}, {disabled: false}];
  const dialog = elements['#delete-confirm-dialog'] = {
    open: false, listeners: [],
    addEventListener(event, handler) { assert.equal(event, 'close'); this.listeners.push(handler); },
    showModal() { this.open = true; },
    finish(value) {
      this.returnValue = value; this.open = false;
      this.listeners.splice(0).forEach(handler => handler());
    }
  };
  const calls = [], messages = [];
  const context = vm.createContext({
    state: {trash: []}, busy: false,
    $: selector => elements[selector], $$: () => restores,
    esc: value => String(value ?? ''), toast: value => messages.push(value),
    api: async (route, body) => { calls.push({route, body: JSON.parse(JSON.stringify(body))}); return {purged: 2, failed: [], remaining: 0}; },
    load: async () => { context.state.trash = []; },
    updateDuplicateControls: () => context.updateTrashControls()
  });
  vm.runInContext(blocks, context);
  return {context, elements, restores, dialog, calls, messages};
}

const rows = () => ['a', 'b'].map(id => ({id, title: id, filename: id + '.pdf', path: id, deleted_at: '2026-10-29T10:00:00+08:00'}));

(async () => {
  {
    const f = fixture();
    f.context.renderTrash();
    assert.equal(f.elements['#empty-trash-button'].disabled, true);
    await f.context.emptyTrash();
    assert.equal(f.dialog.open, false);
    assert.equal(f.calls.length, 0);
  }
  {
    const f = fixture(); f.context.state.trash = rows(); f.context.renderTrash();
    const pending = f.context.emptyTrash();
    assert.equal(f.dialog.open, true);
    assert.match(f.elements['#delete-confirm-message'].textContent, /2 张/);
    assert.match(f.elements['#delete-confirm-note'].textContent, /无法恢复/);
    assert.equal(f.elements['#empty-trash-button'].disabled, true);
    assert.ok(f.restores.every(button => button.disabled));
    await f.context.emptyTrash(); // 二次点击不会发出另一请求。
    f.dialog.finish('cancel'); await pending;
    assert.equal(f.calls.length, 0);
    assert.equal(f.context.state.trash.length, 2);
    assert.equal(f.context.busy, false);
    assert.ok(f.restores.every(button => !button.disabled));
    const normal = f.context.confirmInvoiceDeletion(1);
    assert.equal(f.elements['#delete-confirm-title'].textContent, '确认删除票据');
    assert.match(f.elements['#delete-confirm-note'].textContent, /可以恢复/);
    assert.equal(f.elements['#delete-confirm-submit'].textContent, '确认删除');
    f.dialog.finish('confirm'); assert.equal(await normal, true);
  }
  {
    const f = fixture(); f.context.state.trash = rows();
    const pending = f.context.emptyTrash();
    f.context.state.trash.push({id: 'new'});
    f.context.load = async () => { f.context.state.trash = [{id: 'new'}]; };
    f.context.api = async (route, body) => {
      f.calls.push({route, body: JSON.parse(JSON.stringify(body))});
      return {purged: 2, failed: [], remaining: 1};
    };
    f.dialog.finish('confirm'); await pending;
    assert.deepEqual(f.calls, [{route: '/api/empty-trash', body: {ids: ['a', 'b'], confirmed: true}}]);
    assert.equal(f.context.state.trash[0].id, 'new');
    assert.match(f.messages.at(-1), /永久删除 2 张.*剩余 1 张/);
    assert.equal(f.context.busy, false);
  }
  {
    const f = fixture(); f.context.state.trash = rows();
    f.context.api = async () => ({purged: 1, failed: [{id: 'b', error: '文件被占用'}], remaining: 1});
    f.context.load = async () => { f.context.state.trash = [rows()[1]]; };
    const pending = f.context.emptyTrash(); f.dialog.finish('confirm'); await pending;
    assert.match(f.messages.at(-1), /1 张未删除，可重试/);
    assert.match(f.messages.at(-1), /文件被占用/);
    assert.equal(f.elements['#empty-trash-button'].disabled, false);
  }
  {
    const f = fixture(); f.context.state.trash = rows(); let refreshed = false;
    f.context.api = async () => { throw new Error('连接中断'); };
    f.context.load = async () => { refreshed = true; };
    const pending = f.context.emptyTrash(); f.dialog.finish('confirm');
    await assert.rejects(pending, /连接中断/);
    assert.equal(refreshed, true);
    assert.equal(f.context.busy, false);
    assert.equal(f.elements['#empty-trash-button'].disabled, false);
  }
  console.log('PASS: empty-trash confirmation, cancellation, snapshot, partial failure and retry controls.');
})().catch(error => { console.error(error); process.exitCode = 1; });
