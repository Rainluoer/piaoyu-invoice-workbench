const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const readWeb = name => fs.readFileSync(path.join(__dirname, 'web', name), 'utf8');
const app = readWeb('app.js');
const index = readWeb('index.html');
const formatImportDate = vm.runInNewContext(
  app.slice(app.indexOf('function importDateLabel('), app.indexOf('function renderExtra(')) +
  '\nimportDateLabel'
);

assert.match(index, /data-sort-field="date">开票日期<[^]*?data-sort-field="import">导入日期<[^]*?<th[^>]*>价税合计<\/th>/);
assert.match(app, /esc\(r\.date\)[^\n]*?<\/td><td class="import-time">\$\{importDateLabel\(r\.created_at\)\}<\/td><td class="align-right amount">/);
assert.equal(formatImportDate('2026-11-01T18:02:03Z'), '2026-11-02');
assert.equal(formatImportDate('2026-11-02T02:02:03+08:00'), '2026-11-02');
assert.equal(formatImportDate(null), '未记录');
assert.equal(formatImportDate(''), '未记录');
console.log('PASS: invoice list shows first import date after the issue date, in Beijing time.');
