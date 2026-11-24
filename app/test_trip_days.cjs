const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, 'web/trips.js'), 'utf8');
const count = vm.runInNewContext(script.slice(0, script.indexOf('function renderTripTicket')) + '\ncountTripDays');
const leg = (date, time) => ({date, time});
const first = '2026-11-30', next = '2026-12-01';
assert.equal(count([leg(first, '08:00'), leg(first, '20:12')], '18:00').days, 1);
assert.equal(count([leg(next, '18:00'), leg(first, '18:00')], '18:00').days, 1);
assert.equal(count([leg(first, '08:49'), leg(next, '18:00')], '18:00').days, 2);
assert.equal(count([leg(first, '17:59'), leg(next, '18:00')], '18:00').days, 2);
assert.equal(count([leg(first, '18:30'), leg(next, '18:00')], '19:00').days, 2);
assert.equal(count([leg(first, '22:02'), leg('2026-12-02', '')], '18:00').days, 2);
assert.equal(count([leg(first, ''), leg(next, '18:00')], '18:00').days, undefined);
assert.equal(count([leg('2026-02-30', '18:00'), leg(next, '')], '18:00').days, undefined);
console.log('PASS: same-day, evening, early departure, cutoff changes, multi-day and missing fields.');
