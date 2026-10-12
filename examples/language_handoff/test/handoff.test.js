import test from 'node:test';
import assert from 'node:assert/strict';
import { agents, transferLanguage } from '../public/agents.js';
import * as demo from '../public/agents.js';

const call = (language) => ({ id: 'call-1', name: 'transfer_language', args: { language } });

test('transfers all three languages atomically and waits for broker acceptance', async () => {
  for (const language of Object.keys(agents)) {
    let resolve;
    let sent;
    const client = { sendToolResult: (...args) => {
      sent = args;
      return new Promise((done) => { resolve = done; });
    } };
    let finished = false;
    const result = transferLanguage(client, call(language), language === 'en' ? 'es' : 'en');
    result.then(() => { finished = true; });
    await Promise.resolve();
    assert.equal(finished, false);
    assert.equal(sent[0], 'call-1');
    assert.equal(sent[2].continueWith.voice, agents[language].voice);
    assert.match(sent[2].continueWith.instructions, /transfer_language/);
    assert.equal(sent[2].continueWith.tools[0].name, 'transfer_language', 'incoming agent can transfer back');
    resolve({ status: 'applied' });
    assert.equal(await result, language);
  }
});

test('rejected or timed-out handoffs do not report a new active language', async () => {
  await assert.rejects(transferLanguage({ sendToolResult: async () => ({ status: 'rejected' }) }, call('es'), 'en'), /not applied/);
  await assert.rejects(transferLanguage({ sendToolResult: async () => { throw new Error('timeout'); } }, call('pt'), 'en'), /timeout/);
});

test('the voice demo starts with multilingual listening enabled', () => {
  assert.equal(demo.initialMode?.multilingual, true);
});

test('rejected transfers expose the broker reason and settle the pending tool', async () => {
  const sent = [];
  const client = { sendToolResult: async (...args) => {
    sent.push(args);
    return { status: 'rejected', detail: 'caller speech has priority' };
  } };
  await assert.rejects(transferLanguage(client, call('es'), 'en'), /caller speech has priority/);
  assert.equal(sent.length, 2);
  assert.equal(sent[1][2].outcome, 'failed');
  assert.equal(sent[1][2].continueWith, undefined);
});

test('unsupported languages and unknown tools fail without changing the agent', async () => {
  for (const request of [call('fr'), call('__proto__'), { ...call('es'), name: 'unknown' }]) {
    let sent;
    const result = await transferLanguage({ sendToolResult: (...args) => { sent = args; } }, request, 'en');
    assert.equal(result, 'en');
    assert.equal(sent[2].outcome, 'failed');
    assert.equal(sent[2].continueWith, undefined);
  }
});

test('same-language requests resolve without restarting the agent', async () => {
  let sent;
  assert.equal(await transferLanguage({ sendToolResult: (...args) => { sent = args; } }, call('en'), 'en'), 'en');
  assert.deepEqual(sent[1], { already_active: true });
  assert.equal(sent[2].continueWith, undefined);
});
