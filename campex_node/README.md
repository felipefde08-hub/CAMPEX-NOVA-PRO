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

## Camera Streaming (go2rtc)

Cheap cameras accept only two or three RTSP sessions, and the Node opens one
for vision and one for recording. With `CAMPEX_NODE_GO2RTC=1` the Node runs
[go2rtc](https://github.com/AlexxIT/go2rtc) (MIT) next to it: go2rtc keeps one
connection per camera and vision, recording and the live view read its local
restream. The live page then plays real video (MSE) instead of snapshots;
with detections on it keeps the snapshots, since the boxes are drawn by the
Node.

```bash
python scripts/fetch_go2rtc.py   # pinned release, SHA-256 checked; build.ps1 runs it
export CAMPEX_NODE_GO2RTC=1
export CAMPEX_NODE_GO2RTC_API_PORT=8788    # optional
export CAMPEX_NODE_GO2RTC_RTSP_PORT=8789   # optional
```

go2rtc listens only on 127.0.0.1, asks for a password generated at each start
even from this computer, loads no module that runs commands (exec, echo,
expr) and no WebRTC. Browsers reach the video through the Node at
`/api/cameras/{camera_id}/live`, which checks the login and the page origin.
Its config, with the camera URLs, is `<data_dir>/go2rtc/go2rtc.yaml`; its log
is next to it. When the binary is missing the cameras keep their direct
connection. `GET /api/status` reports it under `streaming`.

## Tracker

`CAMPEX_NODE_VISION_TRACKER` picks how detections get IDs across frames:
`edge` (default, the Node's own tracker) or `bytetrack` (Roboflow's
`trackers` package, Apache-2.0). Compare them on a recording before switching:

```bash
python scripts/compare_trackers.py gravacao.mp4 --out comparacao.mp4
```

On `palace.mp4` (crowd, 0.35 s interval) the edge tracker was more stable:
24 IDs vs 28, 93% vs 77% of detections with an ID.

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

## Accounts and Network Access

The Node is the factory's server: accounts, zones, shifts, events, machine
history and the recording index all live in its SQLite file.

- The desktop launcher listens on the factory network (`0.0.0.0`). Other
  computers open the panel at `http://<node-ip>:<port>/app`; `/api/status`
  lists these addresses in `panel_urls`. Set `CAMPEX_NODE_HOST=127.0.0.1` to
  keep the panel on this computer only.
- The first account is the administrator and can only be created on the
  Node's own computer. The administrator then creates the team's accounts in
  Configurações → Usuários. Nobody signs themselves up afterwards.
- From the network every `/api` call needs a session (the `X-CAMPEX-Session`
  header, or the `campex_session_<port>` cookie that images and videos carry). Calls
  from the Node's own computer, addressed to `127.0.0.1`/`localhost`, do not.
- Passwords are hashed with scrypt; changing one signs the account out
  everywhere.

## Backups

A backup is a copy of the Node database (zones, shifts, events, machine
history, counters, accounts and the recording index). Videos stay in the
recordings folder and are not part of it.

- One copy per day is kept automatically in `CAMPEX_NODE_BACKUP_DIR`
  (default `<data_dir>/backups`); `CAMPEX_NODE_BACKUP_KEEP` sets how many
  (default 14). Point the folder at another disk or a network share to survive
  a failed computer.
- Administrators download a backup or restore one in Fábrica → Configuração
  (`GET /api/backup`, `POST /api/backup/restore`). A restore keeps the current
  database as `antes-da-restauracao-*.sqlite3` first and restarts the Node.

## Run as a Local Service

Service-ready packaging files are available in `packaging/`:

- macOS LaunchAgent: `packaging/macos/install_launch_agent.sh`
- macOS status helper: `packaging/macos/status_launch_agent.sh`
- macOS package build: `packaging/macos/build.sh`
- Linux systemd: `packaging/linux/install_systemd.sh`
- Windows Scheduled Task: `packaging/windows/install_task.ps1`

These scripts do not include tokens or camera credentials. Pair the Node through
the local app at `http://127.0.0.1:8787` or provide secrets through local
environment variables. Full instructions are in `docs/CAMPEX_NODE_SERVICE.md`.

## Automatic Updates (Windows)

The packaged Windows Node updates itself. Every 6 hours
(`CAMPEX_NODE_UPDATE_CHECK_HOURS`) it reads `campex-node-manifest.json` from the
latest GitHub release, checks its Ed25519 signature against the public key
packaged in the Node, downloads the zip, checks its SHA-256 and runs
`campex_node/updates/apply_update.ps1`. The script swaps the install folder,
restarts the Node and restores the previous version if the new one does not
answer on `/api/status` within 4 minutes; a version that was rolled back is not
installed again. Progress is logged to `%LOCALAPPDATA%\CAMPEX\node\logs\campex-node-update.log`
and shown under `update` in `GET /api/status`. Set `CAMPEX_NODE_AUTO_UPDATE=false` to turn it off.

One time, on the publisher's machine:

```powershell
python scripts\release_node.py keygen
```

This writes the private key to `%USERPROFILE%\.campex\campex-node-release.key`
(back it up; never commit it) and the public key to
`campex_node/updates/release_public_key.txt` (commit it). Nodes built without
the public key keep automatic updates off.

For each release:

1. Raise `__version__` in `campex_node/__init__.py`.
2. Run `packaging\windows\build.ps1`; it also writes `dist\campex-node-manifest.json`.
3. Create the GitHub release `campex-node-v<version>`, mark it as latest and
   upload `CampexNode-windows.zip` and `campex-node-manifest.json`.

Installed Nodes pick the release up at their next check.

## Diagnostics

With the local app running, use:

```text
GET http://127.0.0.1:8787/api/diagnostics
```

The diagnostics response includes runtime health, queue size, service state and
camera summary. It intentionally excludes tokens, API keys and RTSP credentials.
