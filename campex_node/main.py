from __future__ import annotations

import argparse
import json

from campex_node.cameras.manager import CameraManager
from campex_node.cloud.client import CloudClient
from campex_node.core.config import NodeSettings
from campex_node.core.lifecycle import NodeLifecycle
from campex_node.core.logger import configure_logging
from campex_node.storage.local_store import LocalStore


def build_lifecycle(settings: NodeSettings | None = None) -> NodeLifecycle:
    settings = settings or NodeSettings.from_env()
    configure_logging(settings)
    store = LocalStore(settings.database_path)
    cloud_client = CloudClient(settings)
    camera_manager = CameraManager(settings)
    return NodeLifecycle(
        settings=settings,
        store=store,
        cloud_client=cloud_client,
        camera_manager=camera_manager,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run CAMPEX Node.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Initialize, send one heartbeat and stop. Useful for validation.",
    )
    args = parser.parse_args(argv)

    lifecycle = build_lifecycle()
    lifecycle.initialize()
    if args.once:
        print(json.dumps(lifecycle.run_once(), indent=2, sort_keys=True))
        return 0
    lifecycle.start()
    lifecycle.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
