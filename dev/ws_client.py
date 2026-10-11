"""Minimal WebSocket client to drive/observe TrainHub from the command line.

Usage:
    uv run python dev/ws_client.py                 # events (snapshot, run.*, …)
    uv run python dev/ws_client.py --logs r7f3k2   # + raw logs of a run
    TRAINHUB_TOKEN=… uv run python dev/ws_client.py --url ws://host:8420/ws
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import websockets


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8420/ws")
    ap.add_argument("--logs", action="append", default=[], help="run_id to follow")
    args = ap.parse_args()
    headers = {}
    if os.environ.get("TRAINHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['TRAINHUB_TOKEN']}"
    async with websockets.connect(args.url, additional_headers=headers, max_size=None) as ws:
        for rid in args.logs:
            await ws.send(json.dumps({"type": "logs.subscribe", "run_id": rid}))
        async for msg in ws:
            if isinstance(msg, bytes):
                sys.stdout.buffer.write(msg[8:])
                sys.stdout.flush()
                continue
            data = json.loads(msg)
            kind = data.get("type")
            if kind == "snapshot":
                runs = data["runs"]
                print(f"\n# snapshot seq={data['seq']} runs={len(runs)}", file=sys.stderr)
            elif kind and kind.startswith("run."):
                run = data.get("run") or {"id": data.get("id"), "status": "-"}
                print(f"\n# {kind} {run['id']} {run['status']}", file=sys.stderr)
            else:
                print(f"\n# {json.dumps(data)[:200]}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
