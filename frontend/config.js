window.CAMPEX_API_BASE_URL =
  window.CAMPEX_API_BASE_URL ||
  (["localhost", "127.0.0.1"].includes(window.location.hostname)
    ? "http://127.0.0.1:8000/api/v1"
    : "https://campexback.vercel.app/api/v1");
