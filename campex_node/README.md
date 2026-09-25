# CAMPEX Node v0.1

CAMPEX Node is the local, long-running service that stays inside the customer's
network. It connects to existing RTSP cameras, keeps camera workers alive, stores
temporary data locally, and prepares heartbeat communication with CAMPEX Cloud.

This version runs a lightweight offline Edge Vision loop using OpenCV HOG for
person detection. It also includes the runtime foundation: configuration,
logging, node identity, camera capture, reconnects, local SQLite storage,
heartbeat delivery, sync outbox, and operational telemetry.

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
export CAMPEX_NODE_CLOUD_URL=""
export CAMPEX_NODE_ID="node_a81f28"
export CAMPEX_NODE_TOKEN="token_privado_do_node"
export CAMPEX_NODE_ORGANIZATION_ID="default"
```

In the product flow, `CAMPEX_NODE_ID` and `CAMPEX_NODE_TOKEN` are returned by
Cloud after the user enters a temporary pairing code in the local Node app.
Do not use user passwords or the backend administrative API token as Node
credentials.

Local runtime settings:

```bash
export CAMPEX_NODE_DATA_DIR="./storage/campex_node"
export CAMPEX_NODE_HEARTBEAT_SECONDS=30
export CAMPEX_NODE_SYNC_SECONDS=10
export CAMPEX_NODE_TELEMETRY_SECONDS=30
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

## Offline Edge Vision

The local Node can process camera frames without internet access when a camera
has `vision_enabled` set to `true`. The first offline detector is intentionally
lightweight:

- Detector: OpenCV HOG person detector
- Device: CPU
- Network/API dependency: none
- Overlay: `GET /api/cameras/{camera_id}/stream?overlay=true`

Useful local endpoints:

```text
POST /api/cameras/{camera_id}/vision/start
POST /api/cameras/{camera_id}/vision/stop
GET  /api/cameras/{camera_id}/vision/status
GET  /api/cameras/{camera_id}/vision/objects
```

## Cloud Behavior

When `CAMPEX_NODE_CLOUD_URL` is missing or unavailable, heartbeat payloads,
metrics, and events are queued in local SQLite as `outbound_events`. The sync
worker retries pending items with backoff and marks delivered items as synced.

The current heartbeat endpoint is prepared as:

```text
POST {CAMPEX_NODE_CLOUD_URL}/node/heartbeat
Authorization: Bearer {CAMPEX_NODE_TOKEN}
```

Node metrics and events are sent to:

```text
POST {CAMPEX_NODE_CLOUD_URL}/node-sync/metrics
POST {CAMPEX_NODE_CLOUD_URL}/node-sync/events
Authorization: Bearer {CAMPEX_NODE_TOKEN}
```

If CAMPEX Cloud is unavailable, the data is stored locally and the Node keeps
running.

## Operational Telemetry

The Node periodically snapshots camera health and queues metrics without running
AI. Current metric types are:

- `camera_online`
- `camera_frames_received`
- `camera_reconnect_attempts`
- `camera_consecutive_failures`

The Node also emits `camera_status_changed` events when a camera changes state.

## Security

Logs sanitize RTSP credentials and error messages before writing them. The Node
never prints camera credentials, API keys or tokens intentionally.

## Run as a Local Service

Service-ready packaging files are available in `packaging/`:

- macOS LaunchAgent: `packaging/macos/install_launch_agent.sh`
- Linux systemd: `packaging/linux/install_systemd.sh`
- Windows Scheduled Task: `packaging/windows/install_task.ps1`

These scripts do not include tokens or camera credentials. Pair the Node through
the local app at `http://127.0.0.1:8787` or provide secrets through local
environment variables. Full instructions are in `docs/CAMPEX_NODE_SERVICE.md`.

## Diagnostics

With the local app running, use:

```text
GET http://127.0.0.1:8787/api/diagnostics
```

The diagnostics response includes runtime health, queue size, service state and
camera summary. It intentionally excludes tokens, API keys and RTSP credentials.
