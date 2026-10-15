"""同步矩阵验证：逐类型单验 + 全开全关并发 + 步进过程。"""
import json
import os
import time
import urllib.request

for _k in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
    os.environ.pop(_k, None)
T = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJjM2RhMzU2ZjE5NDQ0NWQ1YmM1Y2ViNmViYTk3MWI5ZCIsImlhdCI6MTc5MTk3MjQ0MiwiZXhwIjoyMTA3MzMyNDQyfQ.4Tq9lSWBpvEljF9UFhdCuJTM8OicAYz_m46PL0fZFCo"
RES = []


def api(path, payload=None):
    req = urllib.request.Request(
        "http://localhost:8123" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {T}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def st(eid):
    x = [s for s in api("/api/states") if s["entity_id"] == eid]
    return x[0]["state"] if x else "N/A"


def svc(d, s, p):
    api(f"/api/services/{d}/{s}", p)


def check(name, ok, detail=""):
    RES.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


K2, M2 = "switch.ji_dian_qi_2", "switch.ling_huo_kai_guan_4a8c463d03cc_di_2lu"
K3, M3 = "switch.ji_dian_qi_3", "switch.ling_huo_kai_guan_4a8c463d03cc_di_3lu"
ML = "light.shuang_se_diao_guang_deng_ccd257934684"
WL = "light.wled"
MC = "cover.zhi_neng_chuang_lian_cad257934684"
CC = "cover.zhi_neng_chuang_lian_aaaaaa000002"

print("== 1. 开关类单验（mesh↔KNX 双向）==")
svc("switch", "turn_on", {"entity_id": K2}); time.sleep(3)
check("组2 正向 继电器2 on → mesh开关2", st(M2) == "on", f"mesh={st(M2)}")
svc("switch", "turn_off", {"entity_id": M2}); time.sleep(3)
check("组2 反向 mesh开关2 off → 继电器2", st(K2) == "off", f"knx={st(K2)}")
svc("switch", "turn_on", {"entity_id": K3}); time.sleep(3)
check("组3 正向 继电器3 on → mesh开关3", st(M3) == "on", f"mesh={st(M3)}")

print("== 2. 调光组步进（mesh灯 ↔ wled灯）==")
svc("light", "turn_on", {"entity_id": WL, "brightness_pct": 30}); time.sleep(3)
svc("light", "turn_on", {"entity_id": ML, "brightness_pct": 30})
time.sleep(2)
check("灯 同步亮度30%（双方）", st(ML) == "on" and st(WL) == "on", f"mesh={st(ML)} wled={st(WL)}")
for pct in (60, 90, 40):  # 连续步进：中间值不回弹，最终一致
    svc("light", "turn_on", {"entity_id": WL, "brightness_pct": pct}); time.sleep(2.5)
b_ml = [x for x in api("/api/states") if x["entity_id"] == ML][0]["attributes"].get("brightness")
check("灯 步进最终值一致（40%）", abs(int(b_ml or 0) - 102) <= 5, f"mesh brightness={b_ml} (期望≈102)")

print("== 3. 窗帘组（mesh ↔ mock 窗帘）==")
svc("cover", "open_cover", {"entity_id": CC}); time.sleep(3)
check("窗帘 同步打开", st(MC) in ("open", "opening"), f"mesh={st(MC)}")
svc("cover", "stop_cover", {"entity_id": CC}); time.sleep(3)
svc("cover", "close_cover", {"entity_id": CC}); time.sleep(3)
check("窗帘 同步关闭", st(MC) in ("closed", "closing"), f"mesh={st(MC)}")

print("== 4. 全开全关并发（开关2+3+灯 同时动作）==")
svc("switch", "turn_on", {"entity_id": K2})
svc("switch", "turn_on", {"entity_id": K3})
time.sleep(5)
check("全开：继电器2/3 → mesh 开关2/3 全 on",
      st(M2) == "on" and st(M3) == "on", f"{st(M2)}/{st(M3)}")
svc("switch", "turn_off", {"entity_id": K2})
svc("switch", "turn_off", {"entity_id": K3})
svc("light", "turn_off", {"entity_id": WL})
time.sleep(5)
check("全关：mesh 开关2/3 全 off + 灯 off",
      st(M2) == "off" and st(M3) == "off" and st(ML) == "off", f"{st(M2)}/{st(M3)}/{st(ML)}")

ok = sum(1 for _, s, _ in RES if s)
print(f"\n========== 同步矩阵: {ok}/{len(RES)} 通过 ==========")
for name, s, d in RES:
    if not s:
        print("FAIL:", name, d)
