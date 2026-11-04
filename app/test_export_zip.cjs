const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, 'web/app.js'), 'utf8');
const block = source.slice(source.indexOf('async function exportSelectedZip('), source.indexOf('function render(){'));

(async () => {
  const name = '发票_2张_总金额69.12元_2026-11-04.zip';
  const link = {click() { this.clicked = true; }};
  const selected = new Set(['a', 'b']);
  const context = vm.createContext({
    selected, busy: false, state: {key: 'test'}, updateDuplicateControls() {},
    fetch: async (route, options) => {
      assert.equal(route, '/api/export-zip');
      assert.deepEqual(JSON.parse(options.body).ids, ['a', 'b']);
      return {ok: true, headers: {get: () => "attachment; filename*=UTF-8''" + encodeURIComponent(name)}, blob: async () => ({})};
    },
    URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
    document: {createElement: () => link}, setTimeout() {}, toast() {}
  });
  vm.runInContext(block, context);
  await context.exportSelectedZip();
  assert.equal(link.download, name);
  assert.equal(link.clicked, true);
  assert.equal(context.busy, false);
  assert.equal(selected.size, 2);
  link.clicked = false;
  context.fetch = async () => ({ok: false, json: async () => ({error: '所选票据文件不存在'})});
  await assert.rejects(context.exportSelectedZip(), /所选票据文件不存在/);
  assert.equal(link.clicked, false);
  assert.equal(context.busy, false);
  console.log('ZIP download filename and error handling passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
