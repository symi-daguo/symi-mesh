#!/usr/bin/env python3
"""全实体逐一控制 + 串口协议帧比对（真实网关）。

每项：清桥日志 → HA 下发控制 → 抓桥日志 0x30 帧 → 按协议断言 attr/param。
"""
import json
import os
import time
import urllib.request

for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)
T = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJjM2RhMzU2ZjE5NDQ0NWQ1YmM1Y2ViNmViYTk3MWI5ZCIsImlhdCI6MTc5MTk3MjQ0MiwiZXhwIjoyMTA3MzMyNDQyfQ.4Tq9lSWBpvEljF9UFhdCuJTM8OicAYz_m46PL0fZFCo"
BRIDGE_LOG = "/tmp/symi_bridge.log"
SW = "switch.ling_huo_kai_guan_4a8c463d03cc_di_{}lu"
LT = "light.shuang_se_diao_guang_deng_ccd257934684"
CV = "cover.zhi_neng_chuang_lian_cad257934684"
AC = "climate.wen_kong_qi_cdd257934684"
FH = "climate.wen_kong_qi_cdd257934684_di_nuan"

PASS: list[str] = []
FAIL: list[str] = []


def api(path: str, payload: dict) -> None:
    req = urllib.request.Request(
        "http://localhost:8123" + path, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
        method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def svc(domain: str, service: str, payload: dict) -> None:
    api(f"/api/services/{domain}/{service}", payload)


def clear_log() -> None:
    open(BRIDGE_LOG, "w").close()


def grab_frames(addr_lo: int, addr_hi: int) -> list[tuple[int, bytes]]:
    """返回桥日志中该设备地址的所有 (attr, params) 控制记录。

    帧结构: 53 <opcode> <len> <addr_lo> <addr_hi> <attr> <params...> <cksum>
    """
    out = []
    for line in open(BRIDGE_LOG):
        if "TCP>SER 53 30" not in line:
            continue
        hx = line.split("TCP>SER")[1].strip().split()
        if len(hx) < 7:
            continue
        data = bytes(int(x, 16) for x in hx)
        if data[3] == addr_lo and data[4] == addr_hi:
            attr = data[5]
            params = data[6:-1]
            out.append((attr, params))
    return out


def run_case(name: str, call, addr_lo: int, addr_hi: int,
             expect_attr: int, expect_param: bytes, multi_ok: bool = True) -> None:
    clear_log()
    call()
    time.sleep(2.2)
    frames = grab_frames(addr_lo, addr_hi)
    hit = [(a, p) for a, p in frames if a == expect_attr and (not expect_param or p[:1] == expect_param)]
    ok = bool(hit) if multi_ok else len(frames) == 1
    (PASS if ok else FAIL).append(name)
    detail = " | ".join(f"attr=0x{a:02X} p={p.hex()}" for a, p in frames)
    print(("PASS  " if ok else "FAIL  ") + name + f"   [{detail or '无帧'}]")


def sw_service(n: int, service: str):
    return lambda: svc("switch", service, {"entity_id": SW.format(n)})


def main() -> None:
    print("== 零火开关 4 路（addr 0x0100, attr 0x02 打包态）==")
    run_case("开关1 开（0x55→0x56）", sw_service(1, "turn_on"), 0x00, 0x01, 0x02, bytes([0x56]))
    run_case("开关1 关（0x56→0x55）", sw_service(1, "turn_off"), 0x00, 0x01, 0x02, bytes([0x55]))
    run_case("开关2 开（0x55→0x59）", sw_service(2, "turn_on"), 0x00, 0x01, 0x02, bytes([0x59]))
    run_case("开关3 开（0x59→0x69）", sw_service(3, "turn_on"), 0x00, 0x01, 0x02, bytes([0x69]))
    run_case("开关3 关（0x65→0x59）", sw_service(3, "turn_off"), 0x00, 0x01, 0x02, bytes([0x59]))
    run_case("开关2 关（0x59→0x55）", sw_service(2, "turn_off"), 0x00, 0x01, 0x02, bytes([0x55]))
    run_case("开关4 开（0x55→0x95）", sw_service(4, "turn_on"), 0x00, 0x01, 0x02, bytes([0x95]))
    run_case("开关4 关（0x95→0x55）", sw_service(4, "turn_off"), 0x00, 0x01, 0x02, bytes([0x55]))
    run_case("开关1+2 连续开（1.5s 间隔 56→5A）",
             lambda: (sw_service(1, "turn_on")(), time.sleep(1.5), sw_service(2, "turn_on")()),
             0x00, 0x01, 0x02, bytes([0x5A]))
    sw_service(1, "turn_off")(); time.sleep(1)
    sw_service(2, "turn_off")(); time.sleep(1)

    print("== 双色调光灯（addr 0x0104）==")
    run_case("灯 开（attr 0x02=0x02）",
             lambda: svc("light", "turn_on", {"entity_id": LT}), 0x04, 0x01, 0x02, bytes([0x02]))
    run_case("灯 亮度 60%（attr 0x03=60）",
             lambda: svc("light", "turn_on", {"entity_id": LT, "brightness_pct": 60}),
             0x04, 0x01, 0x03, bytes([60]))
    run_case("灯 色温 4000K（attr 0x04=72）",
             lambda: svc("light", "turn_on", {"entity_id": LT, "color_temp_kelvin": 4000}),
             0x04, 0x01, 0x04, bytes([72]))
    run_case("灯 关（attr 0x02=0x01）",
             lambda: svc("light", "turn_off", {"entity_id": LT}), 0x04, 0x01, 0x02, bytes([0x01]))

    print("== 智能窗帘（addr 0x0105）==")
    run_case("窗帘 开（attr 0x05=0x01）",
             lambda: svc("cover", "open_cover", {"entity_id": CV}), 0x05, 0x01, 0x05, bytes([0x01]))
    time.sleep(1)
    run_case("窗帘 停（attr 0x05=0x03）",
             lambda: svc("cover", "stop_cover", {"entity_id": CV}), 0x05, 0x01, 0x05, bytes([0x03]))
    run_case("窗帘 位置 50%（attr 0x06=50）",
             lambda: svc("cover", "set_cover_position", {"entity_id": CV, "position": 50}),
             0x05, 0x01, 0x06, bytes([50]))
    run_case("窗帘 关（attr 0x05=0x02）",
             lambda: svc("cover", "close_cover", {"entity_id": CV}), 0x05, 0x01, 0x05, bytes([0x02]))

    print("== 温控器空调（addr 0x0106）==")
    run_case("空调 开（attr 0x02=0x02）",
             lambda: svc("climate", "set_hvac_mode", {"entity_id": AC, "hvac_mode": "cool"}),
             0x06, 0x01, 0x02, bytes([0x02]))
    run_case("空调 设温 26（attr 0x1B=26）",
             lambda: svc("climate", "set_temperature", {"entity_id": AC, "temperature": 26}),
             0x06, 0x01, 0x1B, bytes([26]))
    run_case("空调 模式 制冷（attr 0x1D=1）",
             lambda: svc("climate", "set_hvac_mode", {"entity_id": AC, "hvac_mode": "cool"}),
             0x06, 0x01, 0x1D, bytes([1]))
    run_case("空调 风速 low（attr 0x1C=3）",
             lambda: svc("climate", "set_fan_mode", {"entity_id": AC, "fan_mode": "low"}),
             0x06, 0x01, 0x1C, bytes([3]))
    run_case("空调 关（attr 0x02=0x01）",
             lambda: svc("climate", "set_hvac_mode", {"entity_id": AC, "hvac_mode": "off"}),
             0x06, 0x01, 0x02, bytes([0x01]))

    print("== 地暖（addr 0x0106）==")
    run_case("地暖 开（attr 0x6B=0x02）",
             lambda: svc("climate", "set_hvac_mode", {"entity_id": FH, "hvac_mode": "heat"}),
             0x06, 0x01, 0x6B, bytes([0x02]))
    run_case("地暖 设温 24（attr 0x6C=24）",
             lambda: svc("climate", "set_temperature", {"entity_id": FH, "temperature": 24}),
             0x06, 0x01, 0x6C, bytes([24]))
    run_case("地暖 关（attr 0x6B=0x01）",
             lambda: svc("climate", "set_hvac_mode", {"entity_id": FH, "hvac_mode": "off"}),
             0x06, 0x01, 0x6B, bytes([0x01]))

    print(f"\n结果: PASS={len(PASS)} FAIL={len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)


if __name__ == "__main__":
    main()
