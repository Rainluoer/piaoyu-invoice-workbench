const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// 运行实际页面筛选函数，覆盖副本状态与原票状态不一致的场景。
const source = fs.readFileSync(path.join(__dirname, 'web', 'app.js'), 'utf8');
const functions = source.slice(source.indexOf('function reimbursementStatus('), source.indexOf('function renderStats()'));
const inputs = {'#search': {value: ''}, '#category-filter': {value: ''}, '#status-filter': {value: ''}, '#sort-filter': {value: 'date-desc'}};
const row = (id, status, extra = {}) => ({id, status, title: id, invoice_no: id, category: '铁路客票', date: '2026-10-15', amount_cents: 1600, missing: false, duplicate_of: null, ...extra});
const context = vm.createContext({
  state: {invoices: [
    row('original-done', '已报销'),
    row('copy-submitted', '报销中', {duplicate_of: 'original-done'}),
    row('original-submitted', '报销中'),
    row('copy-pending', '待报销', {duplicate_of: 'original-submitted'}),
    row('original-pending', '待报销'),
    row('missing-submitted', '报销中', {missing: true})
  ]},
  view: 'all', projectFilter: null, projectKind: 'project', $: selector => inputs[selector]
});
vm.runInContext(functions, context);
const ids = () => Array.from(context.filtered(), item => item.id).sort();
for (const [view, expected] of [
  ['待报销', ['original-pending', 'original-submitted']],
  ['已报销', ['original-done']],
  ['duplicates', ['copy-pending', 'copy-submitted']]
]) {
  context.view = view;
  assert.deepEqual(ids(), expected, view);
}
context.view = 'all';
assert.ok(ids().every(id => !id.startsWith('copy-')));
inputs['#status-filter'].value = '待报销';
assert.deepEqual(ids(), ['original-pending', 'original-submitted']);
assert.equal(context.canonical().length, 3);
console.log('PASS: two-state filters exclude duplicate copies and include legacy unfinished records as pending.');
