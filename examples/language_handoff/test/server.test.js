import test from 'node:test';
import assert from 'node:assert/strict';
import { createApp } from '../server.js';

// Exercise the real Node request handler without needing a listening socket.
async function request(options, url = '/', method = 'GET', headers = {}) {
  const server = createApp(options);
  const result = { headers: {}, status: 200 };
  await server.listeners('request')[0]({ url, method, headers: { host: 'localhost:3000', ...headers } }, {
    setHeader: (name, value) => { result.headers[name] = value; },
    writeHead: (status, headers) => { result.status = status; Object.assign(result.headers, headers); },
    end: (data) => { result.body = data.toString(); },
  });
  return result;
}

test('exchanges the private key and returns only a scoped credential', async () => {
  let upstreamRequest;
  const response = await request({ apiKey: 'test-private-key', fetchImpl: async (url, options) => {
    upstreamRequest = { url, options };
    return Response.json({ api_key: 'scoped-test-key', session_id: 'session-test', expires_in: 7800, internal: 'discard' });
  } }, '/session', 'POST');
  assert.equal(response.status, 201);
  assert.deepEqual(JSON.parse(response.body), { api_key: 'scoped-test-key', session_id: 'session-test', expires_in: 7800 });
  assert.equal(upstreamRequest.url, 'https://api.dialt.com/v1/session-keys');
  assert.equal(upstreamRequest.options.headers.Authorization, 'Bearer test-private-key');
  assert.match(JSON.parse(upstreamRequest.options.body).session_id, /^[\w-]{36}$/);
});

test('serves the screen and SDK assets but never exposes env, source or cross-origin credentials', async () => {
  const options = { apiKey: 'test-private-key', fetchImpl: () => { throw new Error('must not call'); } };
  assert.equal((await request(options)).status, 200);
  assert.equal((await request(options, '/sdk/index.js')).status, 200);
  assert.equal((await request(options, '/sdk/mic-worklet.js')).status, 200);
  for (const path of ['/.env', '/server.js', '/package.json', '/.git/config', '/sdk/../.env']) {
    assert.equal((await request(options, path)).status, 404, path);
  }
  assert.equal((await request(options, '/session', 'POST', { origin: 'https://example.com' })).status, 403);
});

test('credential failures return a useful error without relaying upstream secrets', async () => {
  const response = await request({ apiKey: 'test-private-key',
    fetchImpl: async () => new Response('secret upstream detail', { status: 401 }),
  }, '/session', 'POST');
  assert.equal(response.status, 502);
  assert.deepEqual(JSON.parse(response.body), { error: 'Dialt credential request failed (401).' });
});
