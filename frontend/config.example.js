// On localhost js/api.js finds the CAMPEX Node itself: it is not always on 8787.
window.CAMPEX_API_BASE_URL =
  ["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? ""
    : "https://SEU-BACKEND.vercel.app/api/v1";

window.CAMPEX_NODE_DOWNLOAD_URL =
  "https://github.com/felipefde08-hub/CAMPEX-NOVA-PRO/releases/download/campex-node-local-v1/CampexNode-0.3.2-windows.zip";
