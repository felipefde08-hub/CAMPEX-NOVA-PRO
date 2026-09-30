import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const storage = () => {
  const values = new Map();
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
};
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;

globalThis.localStorage = storage();
globalThis.sessionStorage = storage();
globalThis.window = {
  location: { search: '', protocol: 'https:', hostname: 'campexfront.vercel.app' },
  CAMPEX_API_BASE_URL: 'https://campex-backend.vercel.app/api/v1',
};

const tokenUrl = moduleUrl(await readFile(new URL('../../frontend/js/api-token.js', import.meta.url), 'utf8'));
const mediaUrl = moduleUrl((await readFile(new URL('../../frontend/js/media-auth.js', import.meta.url), 'utf8'))
  .replace('import.meta.url', JSON.stringify('https://campexfront.vercel.app/js/media-auth.js')));
const apiSource = (await readFile(new URL('../../frontend/js/api.js', import.meta.url), 'utf8'))
  .replace('./api-token.js', tokenUrl)
  .replace('./media-auth.js', mediaUrl);
const api = await import(moduleUrl(apiSource));

test('hosted frontend keeps only current Node cameras and never asks Cloud for local poses', async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    if (String(url).startsWith('http://127.0.0.1:8787/api/cameras/local_current/health')) {
      return Response.json({ status: 'OFFLINE', frames_received: 0 });
    }
    if (String(url).startsWith('http://127.0.0.1:8787/api/cameras')) {
      return Response.json([{ id: 'local_current', name: 'Current camera' }]);
    }
    if (String(url).endsWith('/cameras')) {
      return Response.json([{ id: 'local_stale', name: 'Stale camera' }, { id: 'cloud_camera', name: 'Cloud camera' }]);
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  const cameras = await api.listCameras();
  assert.equal(api.getBackendUrl(), 'https://campex-backend.vercel.app/api/v1');
  assert.deepEqual(cameras.map(camera => camera.id), ['cloud_camera', 'local_current'], JSON.stringify(calls));
  assert.deepEqual(await api.getMappingPoses('local_current'), []);
  assert.equal((await api.getCameraHealth('local_current')).status, 'OFFLINE');
  assert.equal(calls.some(url => url.includes('/mapping/poses')), false);
  assert.equal(calls.some(url => url.includes('campex-backend.vercel.app/api/v1/cameras/local_current')), false);
});
