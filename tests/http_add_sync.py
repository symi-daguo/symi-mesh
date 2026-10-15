#!/usr/bin/env python3
"""Create a symi_mesh knx_sync group via HA HTTP config-flow API (3-field picker)."""
import json
import os
import sys
import urllib.request

for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)

TOKEN = sys.argv[1]
MESH = sys.argv[2].split(",") if sys.argv[2] else []
KNX = sys.argv[3].split(",") if sys.argv[3] else []
OTHER = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else []
NAME = sys.argv[5] if len(sys.argv) > 5 else ""
BASE = "http://localhost:8123/api/config/config_entries"


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


flow = call("/flow", {"handler": "symi_mesh"})
fid = flow["flow_id"]
flow = call(f"/flow/{fid}", {"next_step_id": "knx_sync"})
payload = {
    "name": NAME,
    "mesh_entities": MESH,
    "knx_entities": KNX,
    "other_entities": OTHER,
    "direction": "two_way",
    "enabled": True,
    "debounce_ms": 100,
    "outbound_ms": 50,
}
flow = call(f"/flow/{fid}", payload)
print("提交:", flow.get("type"), "| title:", flow.get("title"))
