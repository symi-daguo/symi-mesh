# SYMI 亖米 Mesh 网关（Home Assistant 集成）

通过 **TCP / 串口** 直连 Symi 蓝牙 Mesh 网关，把网关下的全部 Mesh 设备接入 Home Assistant：

- **自动发现**：连接后自动读取网关设备列表（0x12/0x92），新设备运行时上线也会自动创建实体
- **MAC 唯一实体**：所有实体 `unique_id` 由设备 MAC 生成（如 `ccda20b84a89_switch_2`），重装/换机不重复
- **KNX 双向同步**：内置同步引擎，在任意 HA 实体（官方 KNX 实体 ↔ 本集成实体）之间建立双向同步，origin 戳记防回环，生产实测无回弹
- **HACS 就绪**：`hacs.json` + 品牌图标（`brand/`，256/512 + dark 变体）
- 兼容 **Home Assistant 2024.1 及以后**

> 协议与设备模型移植自生产在用的 [node-red-contrib-symi-mesh](https://github.com/symi-daguo/node-red-contrib-symi-mesh)；同步引擎移植自 [symi_two_way_sync](https://github.com/symi-daguo/symi_two_way_sync)。两个通道可并存，互不冲突。

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角 `⋮` → 自定义存储库
2. 仓库地址填 `https://github.com/symi-daguo/symi-mesh-ha`，类别选 **Integration**
3. 下载「SYMI 亖米 Mesh 网关」→ 重启 Home Assistant

### 手动

将 `custom_components/symi_mesh` 复制到 HA 的 `custom_components/` 目录，重启 HA。

## 添加网关

设置 → 设备与服务 → 添加集成 → **SYMI 亖米 Mesh 网关** → 选择「Mesh 网关」：

| 连接方式 | 参数 | 说明 |
|---------|------|------|
| TCP | IP + 端口（默认 4196） | 保存前会实际连通并读取设备列表验证 |
| 串口 | 设备路径 + 波特率（默认 115200） | 见下方「串口接入」 |

连接后自动完成：读取设备列表 → 为每台设备创建实体 → 开启状态上报（0x10）→ 首次全量状态查询（识别三合一面板）。

### 串口接入

- **Linux Docker / HA OS**：串口设备直接透传进 homeassistant 容器（HA OS 的 privileged 核心容器已自动透传 `/dev/ttyUSB0`）。推荐用稳定 by-id 路径（如 `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`）避免设备号漂移；权限默认 `root:audio` 即可读。
- **macOS Docker Desktop**：Docker Desktop 不支持 USB 直通（Linux 容器跑在 VM 内，看不到 macOS 的 `/dev/cu.*`）。官方方案是 USB/IP 桥（`socat`）：

  ```bash
  brew install socat
  socat TCP-LISTEN:4196,fork,reuseaddr OPEN:/dev/cu.wchusbserial8330,rawer,ispeed=115200,ospeed=115200
  ```

  然后在集成里用 **TCP 方式** 连 `host.docker.internal:4196`。桥转发的是真实串口字节流（非模拟设备）。

## 支持的设备

| Mesh 设备类型 | HA 实体 | 说明 |
|--------------|---------|------|
| 零火/单火开关（1-8 路） | `switch` × 路数 | 每路独立实体 |
| 智能插座 | `switch` | |
| 插卡取电 | `switch` + `binary_sensor` | 插卡检测 |
| 双色调光灯 | `light` | 亮度 + 色温 |
| 五色调光灯 | `light` | 亮度 + 色温 + RGB |
| 智能窗帘 | `cover` | 开/关/停（位置依设备上报，未上报时显示 unknown） |
| 温控器 | `climate` | 温度/模式/风速 |
| 温湿度传感器 | `sensor` × 2 | 温度 + 湿度 |
| 门磁 / 人体感应 | `binary_sensor` | |
| 智能门锁 | `lock` | |
| 水浸 / 烟雾报警 | `binary_sensor` | moisture / smoke |
| 离线语控 / 电视盒子 | `sensor`（诊断） | 固件版本等 |
| 情景面板/开关 | `scene` | |
| 三合一面板 | `climate` + `fan` + `climate` | 空调 + 新风 + 地暖，运行时自动识别 |

所有设备通过 `via_device_id` 挂到网关设备下；网关断线时全部实体自动变 `不可用`，恢复后自动重连（指数退避，最长 60s）。

## KNX 双向同步

添加集成 → 选择「**KNX 双向同步**」→ 在实体选择器中**模糊搜索**并点选 ≥2 个实体（支持 HA 已接入的全部实体域，含官方 KNX 集成的 `switch.ji_dian_qi_*` 继电器等）：

- **防回环**：每次写入前打 origin 戳记，回显事件按签名消费；主控权威 + 从端静默 + 组意图闩锁三重保护
- **限速**：本地实体默认防抖 100ms / 出队 50ms；云端实体（米家等）自动放慢
- **方向**：双向（任意一端驱动其余）或单向（仅第一个驱动其余）
- **修改参数**：集成条目右侧齿轮（配置）即可修改实体/方向/防抖/出队间隔，保存后自动重载生效
- 断电/重启后自动恢复

### 三合一（空调 / 新风 / 地暖）对接 KNX

三合一面板在 HA 里是 **3 个实体**，KNX 侧也要建 **3 个实体**，一对一配成 **3 个同步组**：

| Mesh 实体 | 域 | HA 中的名字 | KNX 侧实体 | 说明 |
|-----------|----|------------|-----------|------|
| `climate.<mac>_climate` | climate | 设备名（主实体） | `climate` | 空调：温度 + 模式 + 风速 |
| `fan.<mac>_fresh_air` | fan | 新风 | `fan` | 新风：开关 + 3 档 |
| `climate.<mac>_floor_heating` | climate | 地暖 | `climate` | 地暖：开关 + 设定温度 |

> 一组只放 2 个实体。三个子系统各建一组；把 6 个实体塞进同一组会共用一套
> 组意图闩锁，互相压制。

KNX 侧用 YAML 定义（`configuration.yaml`，组地址按客户 ETS 实际替换）：

```yaml
knx:
  climate:
    # 1) 空调：温度 + 开关 + 模式 + 风速
    - name: "三合一-空调"
      temperature_address: "1/0/1"                  # DPT 9.001 当前室温（只读）
      target_temperature_address: "1/0/2"           # DPT 9.001 设定温度（写）
      target_temperature_state_address: "1/0/3"     # DPT 9.001 设定温度回读
      on_off_address: "1/0/4"                       # DPT 1.001 开关（写）
      on_off_state_address: "1/0/5"                 # DPT 1.001 开关回读
      controller_mode_address: "1/0/6"              # DPT 20.105 自动/制热/制冷/送风/除湿
      controller_mode_state_address: "1/0/7"
      fan_speed_address: "1/0/8"                    # DPT 5.010 档位（写）
      fan_speed_state_address: "1/0/9"
      fan_max_step: 3                               # 关键：三档，与 mesh 一一对应
      fan_zero_mode: "auto"                         # 关键：0 档 = 自动风，与 mesh 的 auto 对齐
      temperature_step: 1
      min_temp: 16
      max_temp: 30

    # 3) 地暖：开关 + 设定温度（不要配 controller_mode，否则模式集合对不上）
    - name: "三合一-地暖"
      temperature_address: "1/2/1"
      target_temperature_address: "1/2/2"
      target_temperature_state_address: "1/2/3"
      on_off_address: "1/2/4"
      on_off_state_address: "1/2/5"
      temperature_step: 1
      min_temp: 18
      max_temp: 32

  fan:
    # 2) 新风：开关 + 3 档
    - name: "三合一-新风"
      switch_address: "1/1/1"                       # DPT 1.001 开关（写）
      switch_state_address: "1/1/2"
      address: "1/1/3"                              # DPT 5.010 档位 1-3（写）
      state_address: "1/1/4"
      max_step: 3                                   # 关键：档位量化到 33/66/100
```

五条硬约束，缺一条就会出现「数值来回跳 / 服务调用报错」：

1. **`fan_max_step: 3` + `fan_zero_mode: "auto"`**：KNX 的 `fan_modes` 才会是
   `["auto","low","medium","high"]`，与 mesh 的同名集合完全一致。默认的
   `fan_zero_mode: off` 会多出一个 mesh 表达不了的 `off`，风速指令会被判为非法值。
2. **`temperature_step: 1`**：mesh 只接受整度，KNX 默认 0.1 会让 22.5 ℃ 落到 22 ℃，
   回读差 0.5 触发一次无效回写。
3. **空调必须用 `controller_mode_address`（DPT 20.105）**：mesh 有 6 种模式
   （制冷/制热/送风/除湿/自动/关），只用 `heat_cool_address` 只能表达冷热两种。
4. **地暖不要配 `controller_mode_address`**：地暖在 mesh 侧只有 `off`/`heat`，
   KNX 侧多出 `cool`/`auto` 时同步会因目标不支持而丢模式。
5. **新风 auto 档在 KNX `fan` 上无对应值**：KNX fan 只有百分比/档位，没有 preset。
   mesh 切到 auto 时按最高档（100%）下发；KNX 侧 0% 视为关机。要保留 auto
   语义，只能用空调 climate 的 `fan_zero_mode: "auto"`，而不是新风 fan。

配好后的检查清单（每项都要在开发者工具里实测）：

- `climate.<空调>` 的 `hvac_modes` 应为 `off/cool/heat/fan_only/dry/auto`，`fan_modes` 为 `auto/low/medium/high`
- `fan.<新风>` 的 `percentage` 只会出现 `0/33/66/100`，`speed_count` 为 3
- `climate.<地暖>` 的 `hvac_modes` 应为 `off/heat`，`min_temp/max_temp` 为 `18/32`

### 高并发与可靠性

| 机制 | 说明 |
|------|------|
| 队列缓存 | 每同步组 `asyncio.Queue(1024)`；同一实体多次变化自动合并只保留最新（丢弃中间态、不丢最终态） |
| 出队限速 | 连续写入按全局出队间隔（默认 50ms）平滑下发，配合协议层帧间隔，KNX/米家每秒 50 个实体变化也能稳定排队同步，不压垮总线 |
| 写锁与并发 | 每实体独立写锁串行化 + 全局并发上限，杜绝交叉写入 |
| 步进保护 | 调光/窗帘渐变过程中的中间值反馈被「从端静默（3s）+ 主控权威（4s）+ 组意图闩锁（4s）」忽略，只认主控期望的最终状态 |
| 绝对值设备 | 开关等绝对值设备同步为最终值，无步进问题 |
| **死循环熔断** | 同一实体 **2 秒内**被本引擎写入 **≥8 次** → 判定死循环，**熔断该实体 60 秒**（其余实体与集成完全不受影响），日志 ERROR + `symi_mesh_loop_breaker` 事件告警，冷却结束自动恢复同步 |

> 官方 KNX 集成负责 KNX 网关与组地址；本集成只同步 HA 实体层，不直连 KNX 总线。

## 服务

| 服务 | 说明 |
|------|------|
| `symi_mesh.scan_devices` | 重新请求设备列表，发现新设备 |
| `symi_mesh.reload` | 重载全部网关与同步组 |
| `symi_mesh.describe` | 同步队列/回环状态写入日志（排障） |

## 开发

```bash
# 协议与设备层单测（无需 HA）
python -m pytest tests/ -q
```

目录结构：

```
custom_components/symi_mesh/
├── manifest.json / strings.json / translations/
├── protocol.py      # 帧编解码（0x53 头 + XOR 校验，纯 Python 可单测）
├── device.py        # 设备模型（MAC↔网络地址、状态机）
├── gateway.py       # asyncio TCP/串口传输 + 指数退避重连
├── coordinator.py   # 连接管理、设备发现、事件分发、命令队列
├── entity.py        # 全平台实体（unique_id=MAC 派生）
├── knx_sync.py      # 双向同步引擎（origin 戳记防回环）
└── config_flow.py   # 网关配置 + KNX 同步配对（宽屏响应式表单）
```

## License

MIT
