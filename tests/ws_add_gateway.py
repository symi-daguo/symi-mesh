#!/usr/bin/env python3
"""Drive the symi_mesh config flow over the HA WebSocket API."""
import asyncio
import json
import os
import sys

# Never route localhost through the sandbox SOCKS proxy.
for _key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_key, None)

import websockets

TOKEN = sys.argv[1]
HOST = sys.argv[2] if len(sys.argv) > 2 else "192.168.65.254"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 4196

WS_URL = "ws://localhost:8123/api/websocket"


async def main() -> None:
    async with websockets.connect(WS_URL, max_size=2**24, proxy=None) as ws:
        # HA sends auth_required first; then we authenticate.
        first = json.loads(await ws.recv())
        print("server:", first.get("type"))
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            print("AUTH FAILED:", msg)
            return
        print("auth_ok, HA", msg.get("ha_version"))

        # Start flow
        await ws.send(json.dumps({"id": 1, "type": "config_flow/start", "handler": "symi_mesh"}))
        msg = json.loads(await ws.recv())
        print("start:", json.dumps(msg, ensure_ascii=False)[:300])
        flow_id = msg["flow_id"]
        step = msg["step_id"]

        # Menu step -> gateway
        if step == "user":
            await ws.send(json.dumps({"id": 2, "type": f"config_flow/flow/{flow_id}", "menu_option": "gateway"}))
            msg = json.loads(await ws.recv())
            print("menu:", msg.get("step_id"), msg.get("type"))
            step = msg.get("step_id")
        else:
            print("unexpected first step:", step)
            return

        # gateway step -> connection type
        if step == "gateway":
            await ws.send(json.dumps({"id": 3, "type": f"config_flow/flow/{flow_id}", "connection_type": "tcp"}))
            msg = json.loads(await ws.recv())
            print("gateway:", msg.get("step_id"), msg.get("type"))
            step = msg.get("step_id")
        else:
            print("unexpected step:", step)
            return

        # tcp params -> create entry
        if step == "gateway_tcp":
            await ws.send(json.dumps({"id": 4, "type": f"config_flow/flow/{flow_id}", "host": HOST, "port": PORT}))
            msg = json.loads(await ws.recv())
            print("tcp result:", json.dumps(msg, ensure_ascii=False)[:500])
            if msg.get("type") == "create_entry":
                print("ENTRY CREATED:", msg.get("title"))
            elif msg.get("type") == "form":
                print("FORM with errors:", msg.get("errors"))
            else:
                print("unexpected:", msg)
            return
        else:
            print("unexpected step:", step)


asyncio.run(main())
