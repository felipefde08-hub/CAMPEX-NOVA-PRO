import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const storage = () => {
  const values = new Map();
  return { getItem: k => values.get(k) ?? null, setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k) };
};
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
globalThis.localStorage = storage();
globalThis.sessionStorage = storage();
globalThis.window = { location: { search: '', protocol: 'http:', hostname: 'localhost' } };
const tokenSource = await readFile(new URL('../../frontend/js/api-token.js', import.meta.url), 'utf8');
const tokenUrl = moduleUrl(tokenSource);
const tokens = await import(tokenUrl);
const mediaUrl = moduleUrl((await readFile(new URL('../../frontend/js/media-auth.js', import.meta.url), 'utf8')).replace('import.meta.url', JSON.stringify('http://localhost/js/media-auth.js')));
const apiSource = (await readFile(new URL('../../frontend/js/api.js', import.meta.url), 'utf8')).replace('./api-token.js', tokenUrl).replace('./media-auth.js', mediaUrl);
const api = await import(moduleUrl(apiSource + "\nexport { requestJson };"));

test('migrates persistent secrets, sends token for protected requests, clears it', async () => {
  localStorage.setItem('campex.api_token', 'test-only-token');
  localStorage.setItem('campex.settings', JSON.stringify({ api_token: 'test-only-token', refresh_ms: 2000 }));
  assert.equal(tokens.getApiToken(), 'test-only-token');
  assert.equal(localStorage.getItem('campex.api_token'), null);
  assert.deepEqual(JSON.parse(localStorage.getItem('campex.settings')), { refresh_ms: 2000 });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response('{}', { status: 200 });
  };
  await api.listCameras();
  await api.testCameraSource({ source_type: 'webcam', source_uri: '0' });
  await api.deleteCamera('test-id');
  await api.analyzeVideo(new Blob(['test']));
  for (const { url, options } of calls) {
    assert.equal(options.headers['X-CAMPEX-Token'], 'test-only-token');
    assert.ok(!url.includes('test-only-token'));
  }
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[3].options.headers['Content-Type'], undefined);
  await api.requestJson('/cameras', { headers: new Headers({ 'X-Test': 'custom' }) });
  assert.equal(calls.at(-1).options.headers['X-CAMPEX-Token'], 'test-only-token');
  assert.equal(calls.at(-1).options.headers['x-test'], 'custom');
  tokens.setApiToken('');
  await api.listCameras();
  assert.equal(calls.at(-1).options.headers['X-CAMPEX-Token'], undefined);
});

test('backend selection is saved only after successful authentication', async () => {
  globalThis.fetch = async () => new Response('{}', { status: 401 });
  await assert.rejects(api.connectBackend('local', 'incorrect'), /Token inválido/);
  assert.equal(localStorage.getItem('campex.backend_url'), null);
  let sent;
  globalThis.fetch = async (url, options) => {
    sent = { url, options };
    return new Response('[]', { status: 200 });
  };
  assert.equal(await api.connectBackend('local', 'test-local-token'), 'http://127.0.0.1:8000/api/v1');
  assert.equal(sent.options.headers['X-CAMPEX-Token'], 'test-local-token');
  assert.equal(localStorage.getItem('campex.backend_url'), 'http://127.0.0.1:8000/api/v1');
  assert.ok(!sent.url.includes('test-local-token'));
});
