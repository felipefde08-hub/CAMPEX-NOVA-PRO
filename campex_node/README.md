# CAMPEX Node v0.1

CAMPEX Node is the local, long-running service that stays inside the customer's
network. It connects to existing RTSP cameras, keeps camera workers alive, stores
temporary data locally, and prepares heartbeat communication with CAMPEX Cloud.

This version does not run AI models. It only builds the runtime foundation:
configuration, logging, node identity, camera capture, reconnects, local SQLite
storage, and heartbeat delivery.

## Configuration

All values are read from environment variables or the repository `.env` file.
Do not put camera passwords, RTSP credentials, API keys or tokens in public
files.

Required for real cameras:

```bash
export CAMPEX_NODE_CAMERAS_JSON='[
  {
    "id": "cam_cut_1",
    "name": "Maquina de corte 1",
    "rtsp_url": "rtsp://usuario:senha@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0",
    "enabled": true
  }
]'
```

Optional Cloud settings:

```bash
export CAMPEX_NODE_CLOUD_URL="https://campexback.vercel.app/api/v1"
export CAMPEX_NODE_TOKEN="token_privado_do_node"
export CAMPEX_NODE_CLOUD_API_TOKEN="token_privado_do_backend"
export CAMPEX_NODE_ORGANIZATION_ID="default"
```

Local runtime settings:

```bash
export CAMPEX_NODE_DATA_DIR="./storage/campex_node"
export CAMPEX_NODE_HEARTBEAT_SECONDS=30
export CAMPEX_NODE_CAMERA_RECONNECT_SECONDS=5
```

## Run Locally

From the repository root:

```bash
python -m campex_node.main --once
python -m campex_node.main
python -m campex_node.main --app
```

`--once` initializes the node, sends one heartbeat attempt, prints the payload,
and stops. Running without `--once` keeps the service alive for 24/7 operation.
`--app` opens the lightweight local app at `http://127.0.0.1:8787`.

## Cloud Behavior

When `CAMPEX_NODE_CLOUD_URL` is missing or unavailable, heartbeat payloads are
queued in local SQLite as `outbound_events`. The future Cloud sync worker can
reuse this outbox.

The current heartbeat endpoint is prepared as:

```text
POST {CAMPEX_NODE_CLOUD_URL}/node/heartbeat
Authorization: Bearer {CAMPEX_NODE_TOKEN}
```

If CAMPEX Cloud does not expose that endpoint yet, the heartbeat is stored
locally and the Node keeps running.

## Security

Logs sanitize RTSP credentials and error messages before writing them. The Node
never prints camera credentials, API keys or tokens intentionally.

## Service Readiness

The process is structured for future service wrappers:

- Linux: systemd unit running `python -m campex_node.main`
- Windows: Windows Service wrapper running the same module

Installers are intentionally outside this first version.
