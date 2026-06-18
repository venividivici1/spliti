// Behavioural tests for the offline-sync engine in spliti/static/index.html.
//
// The sync code is an inline <script> tightly coupled to the DOM, so rather than
// reimplement it (which would test nothing), we extract the *real* source of the
// functions under test and evaluate them in a vm sandbox with stubbed globals.
// That way these assertions break if the actual shipped logic regresses.
//
// Run: node --test tests/sync_sim.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HTML = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'spliti', 'static', 'index.html'),
  'utf8',
);

// Pull a top-level `function name(... ) { ... }` out of the file. The body is
// indented, so the function's own closing brace is the first `}` at column 0.
function extract(decl) {
  const re = new RegExp(decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '[\\s\\S]*?\\n\\}');
  const m = HTML.match(re);
  if (!m) throw new Error('could not extract: ' + decl);
  return m[0];
}

const flushSrc = extract('async function flush() {');
const detailSigSrc = extract('function detailSig(d) {');

// Build a sandbox that defines the real flush()/detailSig() against stubs we can
// inspect. `let flushing` is declared in the same script so flush's closure binds
// to it; everything else flush touches is provided as a sandbox global.
function makeEnv({ online }) {
  const calls = { api: [], renders: 0 };
  const fresh = {
    group: { id: 1 },
    members: [{ id: 1, name: 'Ada' }, { id: 2, name: 'Bo' }],
    // a NEW expense another member added while we were "offline"
    expenses: [{ id: 99, amount_paise: 3000, deleted: false }],
    settlements: [],
    balances: [], suggestions: [],
  };
  const sandbox = {
    navigator: { onLine: online },
    outbox: [],
    saveOutbox() {},
    sendOp() {},
    saveSnapshot() {},
    renderMain() { calls.renders++; },
    updateNetBadge() {},
    toast() {},
    console,
    state: {
      current: 1,
      overview: { 1: { text: 'cached' } },
      // cached snapshot has NO expenses yet — so a pull must change the signature
      detail: { group: { id: 1 }, members: fresh.members, expenses: [], settlements: [] },
    },
    async api(method, path) { calls.api.push(method + ' ' + path); return fresh; },
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `let flushing = false;\n${detailSigSrc}\n${flushSrc}\nthis.flush = flush;`,
    sandbox,
  );
  return { sandbox, calls, fresh };
}

test('flush pulls fresh server state even when navigator.onLine is false', async () => {
  // This is the reconnect-not-pulling bug: navigator.onLine can stay false after
  // a real reconnect, so flush must attempt the fetch regardless.
  const { sandbox, calls, fresh } = makeEnv({ online: false });
  await sandbox.flush();
  assert.ok(calls.api.includes('GET /current'), 'should fetch /current despite onLine=false');
  assert.equal(sandbox.state.detail, fresh, 'state.detail adopts the freshly pulled data');
  assert.equal(calls.renders, 1, 're-renders because the data changed');
});

test('flush re-renders only when the pulled data actually changed', async () => {
  const { sandbox, calls } = makeEnv({ online: true });
  // Make the cached snapshot identical to what the server returns.
  sandbox.state.detail = { group: { id: 1 }, members: sandbox.state.detail.members,
    expenses: [{ id: 99, amount_paise: 3000, deleted: false }], settlements: [] };
  await sandbox.flush();
  assert.ok(calls.api.includes('GET /current'), 'still pulls');
  assert.equal(calls.renders, 0, 'no re-render when the signature is unchanged');
});

test('flush is a single-flight: overlapping calls do not double-fetch', async () => {
  const { sandbox, calls } = makeEnv({ online: true });
  await Promise.all([sandbox.flush(), sandbox.flush()]);
  assert.equal(calls.api.filter(c => c === 'GET /current').length, 1, 'second call is guarded out');
});
