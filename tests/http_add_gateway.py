#!/usr/bin/env python3
"""Add the symi_mesh gateway via HA's HTTP config-flow API (urllib, no proxy)."""
import json
import os
import sys
import urllib.request

# Never route localhost through a sandbox proxy.
for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)

TOKEN = sys.argv[1]
HOST = sys.argv[2] if len(sys.argv) > 2 else "192.168.65.254"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 4196
BASE = "http://localhost:8123/api/config/config_entries"


def call(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


flow = call("/flow", {"handler": "symi_mesh"})
print("STEP1 menu:", flow.get("type"), flow.get("step_id"), flow.get("menu_options"))
fid = flow["flow_id"]

flow = call(f"/flow/{fid}", {"next_step_id": "gateway"})
print("STEP2 gateway:", flow.get("type"), flow.get("step_id"))

flow = call(f"/flow/{fid}", {"connection_type": "tcp"})
print("STEP3 tcp:", flow.get("type"), flow.get("step_id"))

flow = call(f"/flow/{fid}", {"host": HOST, "port": PORT})
print("STEP4 result:", json.dumps(flow, ensure_ascii=False))
if flow.get("type") == "create_entry":
    print("ENTRY CREATED:", flow.get("title"))
elif flow.get("type") == "form":
    print("ERRORS:", flow.get("errors"))
