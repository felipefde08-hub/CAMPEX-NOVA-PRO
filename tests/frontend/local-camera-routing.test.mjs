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

test('on the Node computer, Cloud cameras keep the Cloud copy that says they come through the relay', async () => {
  globalThis.fetch = async (url) => {
    if (String(url).startsWith('http://127.0.0.1:8787/api/cameras')) {
      // The local Node also lists the Cloud cameras it captures.
      return Response.json([{ id: 'cam_cloud', name: 'Doca', health: { status: 'ONLINE' } }]);
    }
    if (String(url).endsWith('/cameras')) {
      return Response.json([{ id: 'cam_cloud', name: 'Doca', health: { status: 'ONLINE', runtime: 'campex_node' } }]);
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  const cameras = await api.listCameras();

  assert.equal(cameras.length, 1);
  assert.equal(cameras[0].health.runtime, 'campex_node');
  assert.equal(cameras[0].runtime, undefined);
});

test('finds the local Node when it is not on port 8787 and remembers the port', async () => {
  const nodeOrigin = 'http://127.0.0.1:8790';
  globalThis.fetch = async (url) => {
    const target = String(url);
    if (target === `${nodeOrigin}/api/status`) return Response.json({ node_id: 'node_1' });
    if (target.startsWith(`${nodeOrigin}/api/cameras`)) return Response.json([{ id: 'local_dock', name: 'Doca' }]);
    if (target.startsWith('http://127.0.0.1:')) throw new TypeError('Failed to fetch');
    if (target.endsWith('/cameras')) return Response.json([]);
    throw new Error(`Unexpected request: ${url}`);
  };

  const cameras = await api.listCameras();

  assert.deepEqual(cameras.map(camera => camera.id), ['local_dock']);
  assert.equal(api.localNodeOrigin(), nodeOrigin);
  assert.equal(localStorage.getItem('campex.local_node_port'), '8790');
});

test('a dashboard without a local Node does not rescan the ports on every refresh', async () => {
  let probes = 0;
  globalThis.fetch = async (url) => {
    const target = String(url);
    if (target.endsWith('/api/status')) probes += 1;
    if (target.startsWith('http://127.0.0.1:')) throw new TypeError('Failed to fetch');
    if (target.endsWith('/cameras')) return Response.json([]);
    throw new Error(`Unexpected request: ${url}`);
  };

  await api.listCameras();
  const afterFirstScan = probes;
  await api.listCameras();

  assert.equal(afterFirstScan, 20);
  assert.equal(probes, afterFirstScan);
});
