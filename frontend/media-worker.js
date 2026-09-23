// No credentials or media are persisted or cached by this worker.
self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

function credentials(client, url) {
  return new Promise((resolve, reject) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => {
      channel.port1.close();
      reject(new Error('Media authorization timed out'));
    }, 5000);
    channel.port1.onmessage = ({ data }) => {
      clearTimeout(timer);
      channel.port1.close();
      if (data.error) reject(new Error('Media destination rejected'));
      else resolve(data.token);
    };
    client.postMessage({ type: 'campex-media-auth', url }, [channel.port2]);
  });
}

async function mediaResponse(event, url) {
  try {
    if (event.request.method !== 'GET') return new Response(null, { status: 405 });
    const client = await self.clients.get(event.clientId);
    if (!client) return new Response(null, { status: 401 });
    const target = new URL(url.searchParams.get('url'));
    // Extra parameters added by viewers (e.g. cache-busting t) are forwarded.
    for (const [key, value] of url.searchParams) {
      if (key !== 'url') target.searchParams.set(key, value);
    }
    const token = await credentials(client, target.href);
    const headers = new Headers();
    if (token) headers.set('X-CAMPEX-Token', token);
    const range = event.request.headers.get('Range');
    if (range) headers.set('Range', range);
    return await fetch(target.href, {
      headers, signal: event.request.signal, cache: 'no-store',
      credentials: 'omit', redirect: 'error',
    });
  } catch {
    return new Response('Não foi possível carregar a mídia protegida.', {
      status: 502, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
    });
  }
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const proxy = new URL('./__campex_media__', self.location.href);
  if (url.origin === proxy.origin && url.pathname === proxy.pathname) {
    event.respondWith(mediaResponse(event, url));
  }
});
