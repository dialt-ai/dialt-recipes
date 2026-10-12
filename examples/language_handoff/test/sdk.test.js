import test from 'node:test';
import assert from 'node:assert/strict';
import { DialtClient } from '@dialt/sdk';
import { agentConfig, initialMode, transferLanguage } from '../public/agents.js';

class Socket extends EventTarget {
  readyState = 0;
  sent = [];
  constructor() {
    super();
    queueMicrotask(() => { this.readyState = 1; this.dispatchEvent(new Event('open')); });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  emit(data) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(data) })); }
  close() { this.readyState = 3; }
}

test('the pinned Dialt SDK sends the atomic transfer and applies the acknowledged voice', async (t) => {
  const client = new DialtClient({
    mode: initialMode,
    WebSocketImpl: Socket,
    autoReconnect: false,
    player: { stop() {}, clear() {}, pendingMs: () => 0 },
  });
  t.after(() => client.close());
  const connecting = client.connect();
  await Promise.resolve();
  client.ws.emit({ type: 'ready' });
  await connecting;
  assert.equal(client.ws.sent[0].mode.multilingual, true, 'SDK must send multilingual in the actual start frame');

  for (const language of ['es', 'pt', 'en']) {
    const transferring = transferLanguage(client,
      { id: `call-${language}`, name: 'transfer_language', args: { language } },
      language === 'en' ? 'pt' : 'en');
    const frame = client.ws.sent.at(-1);
    assert.equal(frame.type, 'tool_result');
    assert.equal(frame.continue_with.voice, agentConfig(language).voice);
    assert.equal(frame.continue_with.tools[0].name, 'transfer_language');
    client.ws.emit({ type: 'handoff_ack', operation_id: frame.continue_with.operation_id,
      status: 'applied', accepted: true });
    assert.equal(await transferring, language);
    assert.equal(client.mode.voice, agentConfig(language).voice);
    assert.equal(client.mode.instructions, agentConfig(language).instructions);
    assert.equal(client.mode.multilingual, true, 'multilingual listening survives the voice handoff');
  }
});
