const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

(async () => {
  const scheduled = [], messages = [];
  let attempts = 0, loaded = 0;
  const context = vm.createContext({
    document: {addEventListener() {}},
    state: {receipt_jobs: []}, view: 'all',
    setTimeout(fn, delay) { scheduled.push({fn, delay}); return scheduled.length; },
    api: async () => {
      attempts++;
      if (attempts <= 2) throw Error('temporary disconnect');
      return {id: 'job', status: 'done', item_count: 4, total_cents: 33439};
    },
    load: async () => { loaded++; },
    fullMoney: value => String(value),
    toast: message => messages.push(message)
  });
  vm.runInContext(fs.readFileSync('web/receipts.js', 'utf8'), context);
  context.scheduleReceiptPoll('job');
  assert.equal(scheduled[0].delay, 800);
  await scheduled[0].fn();
  assert.equal(scheduled[1].delay, 1600);
  await scheduled[1].fn();
  assert.equal(scheduled[2].delay, 3200);
  assert.equal(messages.filter(message => message.includes('自动重试')).length, 1);
  await scheduled[2].fn();
  assert.equal(loaded, 1);
  assert.equal(scheduled.length, 3);
  assert.equal(context.state.receipt_jobs[0].status, 'done');
  console.log('PASS: interrupted receipt polling retries and recovers without duplicate tasks.');
})().catch(error => { console.error(error); process.exitCode = 1; });
