#!/usr/bin/env python3
"""Add a symi_mesh knx_sync group via HA HTTP config-flow API."""
import json
import os
import sys
import urllib.request

for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)

TOKEN = sys.argv[1]
ENTITIES = sys.argv[2].split(",") if len(sys.argv) > 2 else [
    "switch.ling_huo_kai_guan_4a8c463d03cc_di_1lu",
    "input_boolean.symi_sync_test",
]
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
print("knx_sync step:", flow.get("type"), flow.get("step_id"))
flow = call(
    f"/flow/{fid}",
    {
        "name": "开关1 ↔ 同步测试",
        "entities": ENTITIES,
        "direction": "two_way",
        "enabled": True,
        "debounce_ms": 100,
        "outbound_ms": 50,
    },
)
print("result:", json.dumps(flow, ensure_ascii=False)[:400])
if flow.get("type") == "create_entry":
    print("SYNC GROUP CREATED:", flow.get("title"))
elif flow.get("type") == "form":
    print("ERRORS:", flow.get("errors"))
