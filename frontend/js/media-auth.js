// Native media elements cannot set authentication headers. A same-origin
// service worker forwards their requests with the current tab's credential.
export async function createMediaUrl(apiBaseUrl, getToken) {
  const workerUrl = new URL('../media-worker.js', import.meta.url);
  const proxyUrl = new URL('./__campex_media__', workerUrl);
  let available = false;
  if (globalThis.navigator?.serviceWorker) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data?.type !== 'campex-media-auth' || !event.ports[0]) return;
      if (event.source?.scriptURL !== workerUrl.href) return;
      try {
        const target = new URL(event.data.url);
        const base = new URL(`${apiBaseUrl}/`);
        const path = target.pathname.slice(base.pathname.length);
        const permitted = /^(cameras\/[^/]+\/(stream|video|snapshot)|videos\/[^/]+\/(video|debug-video)|events\/[^/]+\/evidence|operations\/stream)$/.test(path);
        if (target.origin !== base.origin || !target.pathname.startsWith(base.pathname) || !permitted) {
          throw new Error('Invalid media destination');
        }
        event.ports[0].postMessage({ token: getToken() });
      } catch {
        event.ports[0].postMessage({ error: true });
      }
    });
    try {
      await navigator.serviceWorker.register(workerUrl, { scope: new URL('./', workerUrl).pathname });
      await new Promise((resolve, reject) => {
        const check = () => {
          if (navigator.serviceWorker.controller?.scriptURL === workerUrl.href) {
            clearTimeout(timer);
            navigator.serviceWorker.removeEventListener('controllerchange', check);
            resolve();
          }
        };
        const timer = setTimeout(() => {
          navigator.serviceWorker.removeEventListener('controllerchange', check);
          reject(new Error('Media worker activation timeout'));
        }, 8000);
        navigator.serviceWorker.addEventListener('controllerchange', check);
        check();
      });
      available = true;
    } catch {
      // API calls remain usable when browser policy blocks service workers.
    }
  }
  return (url) => {
    if (!available) {
      if (getToken()) throw new Error('Para abrir mídia protegida, use HTTPS ou localhost e permita service workers no navegador.');
      return url;
    }
    const proxy = new URL(proxyUrl);
    proxy.searchParams.set('url', url);
    return proxy.href;
  };
}
