import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

const publicRoot = new URL('./public/', import.meta.url);
const sdkRoot = new URL('./node_modules/@dialt/sdk/src/', import.meta.url);
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.ttf': 'font/ttf', '.wasm': 'application/wasm' };

// Local demo only: never expose the persistent key or serve the repository root.
export function createApp({ apiKey = process.env.DIALT_API_KEY, fetchImpl = fetch } = {}) {
  return createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    const json = (status, data) => {
      res.writeHead(status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(data));
    };
    try {
      const { pathname } = new URL(req.url, 'http://localhost');
      if (pathname === '/session' && req.method === 'POST') {
        // Disallow cross-site requests to the local credential endpoint.
        if (req.headers.origin && req.headers.origin !== `http://${req.headers.host}`) {
          return json(403, { error: 'Cross-origin request refused.' });
        }
        if (!apiKey) return json(503, { error: 'Set DIALT_API_KEY in .env.' });
        const upstream = await fetchImpl('https://api.dialt.com/v1/session-keys', {
          method: 'POST',
          headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: randomUUID() }),
          signal: AbortSignal.timeout(15000),
        });
        if (!upstream.ok) return json(502, { error: `Dialt credential request failed (${upstream.status}).` });
        const { api_key, session_id, expires_in } = await upstream.json();
        if (!api_key || !session_id) return json(502, { error: 'Invalid Dialt credential response.' });
        return json(201, { api_key, session_id, expires_in });
      }
      if (req.method !== 'GET') return json(405, { error: 'Method not allowed.' });
      const isSdk = pathname.startsWith('/sdk/');
      const relative = pathname === '/' ? 'index.html' : pathname.slice(isSdk ? 5 : 1);
      const root = isSdk ? sdkRoot : publicRoot;
      const file = new URL(relative, root);
      const extension = file.pathname.slice(file.pathname.lastIndexOf('.'));
      if (!file.href.startsWith(root.href) || !types[extension] ||
          (isSdk && !['.js', '.wasm'].includes(extension))) {
        return json(404, { error: 'Not found.' });
      }
      const content = await readFile(file);
      res.writeHead(200, { 'Content-Type': types[extension] });
      res.end(content);
    } catch (error) {
      json(error.code === 'ENOENT' ? 404 : 502, {
        error: error.code === 'ENOENT' ? 'Not found.' : 'Could not reach Dialt. Please try again.',
      });
    }
  });
}

if (import.meta.main) {
  const port = Number(process.env.PORT || 3000);
  createApp().listen(port, '127.0.0.1', () => console.log(`Dialt demo: http://localhost:${port}`));
}
