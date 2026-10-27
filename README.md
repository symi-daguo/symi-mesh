# SYMI 亖米 Mesh 网关（Home Assistant 集成）

通过 **TCP / 串口** 直连 Symi 蓝牙 Mesh 网关，把网关下的全部 Mesh 设备接入 Home Assistant：

- **自动发现**：连接后自动读取网关设备列表（0x12/0x92），新设备运行时上线也会自动创建实体
- **MAC 唯一实体**：所有实体 `unique_id` 由设备 MAC 生成（如 `ccda20b84a89_switch_2`），重装/换机不重复
- **KNX 双向同步**：内置同步引擎，在任意 HA 实体（官方 KNX 实体 ↔ 本集成实体）之间建立双向同步，origin 戳记防回环，生产实测无回弹
- **HACS 就绪**：`hacs.json` + 品牌图标（`brand/`，256/512 + dark 变体）
- 兼容 **Home Assistant 2024.8 及以后**（用到类型化 `ConfigEntry` 与 `AddConfigEntryEntitiesCallback`）

> 协议与设备模型移植自生产在用的 [node-red-contrib-symi-mesh](https://github.com/symi-daguo/node-red-contrib-symi-mesh)；同步引擎移植自 [symi_two_way_sync](https://github.com/symi-daguo/symi_two_way_sync)。两个通道可并存，互不冲突。

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角 `⋮` → 自定义存储库
2. 仓库地址填 `https://github.com/symi-daguo/symi-mesh`，类别选 **Integration**
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

### 一个实体参与多个同步组（星型中转）

同一个实体可以同时出现在多个同步组里，用来做「一台 mesh 设备 ↔ 多套 KNX 实体」的中转。典型场景：客户 KNX 触摸屏控制不到空调网关，于是两侧各建一套 KNX 实体，都跟同一台 mesh 三合一同步。

| 组名 | 实体 |
|------|------|
| 空调-网关侧 | `climate.knx_空调_网关` ↔ `climate.mesh_三合一` |
| 空调-屏幕侧 | `climate.knx_空调_屏幕` ↔ `climate.mesh_三合一` |

三方向实测（6 秒观察窗，无回弹、无熔断）：

| 操作 | 写入次数 | 最终结果 |
|------|---------|---------|
| 改网关侧实体 | 2（mesh + 屏幕侧） | 三端一致 |
| 改 mesh 实体 | 2（两侧各一次） | 三端一致 |
| 改屏幕侧实体 | 2（mesh + 网关侧） | 三端一致 |

三条约束：

1. **每组只放 2 个实体**。组内多实体共用一套组意图闩锁（`GROUP_LATCH`），互相压制。
2. **两个 KNX 实体不要共用同一个 `state_address`**。共用会形成双回显，虽然值相同时会被跳过，但总线上会多出一倍报文。
3. **两个 KNX 实体的 `fan_max_step` / `fan_zero_mode` 必须一致**，否则同一个风速值在两侧的语义不同，会出现「一侧改了、另一侧又拉回去」。

环形（A↔M、M↔B、B↔A）实测也能收敛（2 次写入、0 次熔断，因为值幂等且回显被消费），但会放大总线流量，不建议刻意构造。真出现「对端反复改写同一个值」的振荡，2 秒 8 次的熔断会兜底。

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
| `symi_mesh.trigger_scene` | 下发 mesh 场景号（`scene_id` 1-255，`transparent` 走 0x34 透传） |

## 官方收录状态

### HACS 默认仓库

已满足 [HACS 收录条件](https://hacs.xyz/docs/publish/include/)，只差发布 Release 后向 `hacs/default` 提交 PR：

| 条件 | 状态 |
|------|------|
| 仓库公开、有 description / issues / topics | 满足 |
| `hacs.json` 含 `name` | 满足 |
| brand 目录含 `icon.png` | 满足 |
| MIT 许可证 | 满足 |
| HACS Action 通过 | 满足（GitHub Actions 最近一次 success） |
| Hassfest 通过 | 满足（同上） |
| 有 GitHub Release | 需把 Release 打到当前版本 |

### Home Assistant 核心仓库

进入 core 的门槛是 [Bronze 级全部 20 条规则](https://contributors.home-assistant.io/core/quality-scale/rules/#bronze)。逐条状态记在 `custom_components/symi_mesh/quality_scale.yaml`，当前：**done 13 条、exempt 3 条、todo 4 条**。

| 状态 | 规则 |
|------|------|
| 满足（13） | `action-setup`、`common-modules`、`config-flow`、`config-flow-test-coverage`、`dependency-transparency`、`docs-actions`、`entity-event-setup`、`entity-unique-id`、`has-entity-name`、`runtime-data`、`test-before-configure`、`test-before-setup`、`unique-config-entry` |
| 豁免（3） | `appropriate-polling`（local_push 不轮询）、`docs-conditions`、`docs-triggers`（不提供 trigger/condition） |
| 待补（4） | `brands`（需提交到 home-assistant/brands）、`docs-high-level-description`、`docs-installation-instructions`、`docs-removal-instructions`（需写到 home-assistant.io 文档仓库） |

补齐顺序：

1. 文档三条：在 `home-assistant.io` 仓库新增 `source/_integrations/symi_mesh.markdown`（内容可从 README 的「支持的设备」「安装」「添加网关」平移）。
2. `brands`：向 `home-assistant/brands` 提交 `custom_integrations/symi_mesh/`。
3. 全部 `done` 后，把代码从 `custom_components/` 迁到 `homeassistant/components/symi_mesh/` 提 core PR。

## 开发

```bash
# 协议/设备层单测（无需 HA）+ 配置流测试（需 pytest-homeassistant-custom-component）
uv run --with pytest pytest tests -q                      # 20 例，纯 Python
uv run --with pytest --with pytest-homeassistant-custom-component pytest -q   # 全部 26 例
```

目录结构：

```
custom_components/symi_mesh/
├── manifest.json / quality_scale.yaml / strings.json / translations/
├── protocol.py      # 帧编解码（0x53 头 + XOR 校验，纯 Python 可单测）
├── device.py        # 设备模型（MAC↔网络地址、状态机、三合一取证）
├── gateway.py       # asyncio TCP/串口传输 + 指数退避重连 + 首连校验
├── coordinator.py   # 连接管理、设备发现、事件分发、命令队列
├── runtime.py       # 类型化 ConfigEntry 运行时数据（runtime_data）
├── entity.py        # 全平台实体（unique_id=MAC 派生）
├── knx_sync.py      # 双向同步引擎（origin 戳记防回环）
└── config_flow.py   # 网关配置 + KNX 同步配对（宽屏响应式表单）

tests/
├── test_protocol.py     # 帧编解码 / 校验 / 分片（11 例）
├── test_device.py       # 三合一取证 / 状态位 / 无效值（9 例）
└── test_config_flow.py  # 配置流成功/失败/重复（6 例）
```

## 变更记录

### 1.2.17
- `services.yaml` 补齐 `trigger_scene`（代码注册了 4 个服务，文档只写了 3 个）
- 仓库清理：删除与 `brand/` 完全重复的根目录 `icon.png` / `logo.png`；`.workbuddy/` 与 `.pytest_cache/` 加入 `.gitignore`
- README：最低支持版本修正为 HA 2024.8（类型化 `ConfigEntry` + `AddConfigEntryEntitiesCallback`），目录结构补 `runtime.py` 与 `tests/`

### 1.2.16
- 运行时数据迁到 `ConfigEntry.runtime_data`（`runtime.py` 新增类型化 `SymiMeshRuntime`），不再用 `hass.data[DOMAIN][entry_id]`，满足 Bronze `runtime-data`
- 新增 `ConfigEntryNotReady`：网关首次连接失败时由 HA 退避重试，而不是静默后台重连，满足 `test-before-setup`
- 平台统一改签 `AddConfigEntryEntitiesCallback` + 类型化条目，服务注册改用 `has_service` 幂等判断
- 修 `trigger_scene` 服务在最后一个条目卸载时未被移除的问题
- 新增 `quality_scale.yaml`，逐条登记 Bronze 状态

### 1.2.15
- 串口枚举 `list_ports.comports()` 移入执行器线程，消除 HA 日志里的 `Detected blocking call to glob/scandir` 告警（配置串口网关时必现）
- `serialx` 首次导入同样走执行器并缓存，消除 `gateway.py` 的 `import_module` 阻塞告警

### 1.2.14
- 地暖设定温度解析范围 `18..32` 放宽到 `5..40`：设备上报 16（部分机型下限）或 0 不再被丢弃
- `target_temperature` 增加「上次设定值」兜底（地暖 22 / 空调 24），滑条始终可拖
- `0x16` 室温上报 `0x0000`（面板未接室温探头）按无效值丢弃，不再显示 0.0 ℃

### 1.2.13 / 1.2.12
- 空调风速跨协议归一：KNX 的 `off / low / 0% / 33% / 1 / 2 / 3` 与 mesh 的 `low / medium / high / auto` 互转，目标表达不了的档位丢弃而不是硬发
- 温度与风速拆成多次服务调用（`climate.set_temperature` 不接受 `fan_mode`，原实现会静默丢掉风速）
- 重启后以组内第一个实体为准自动对齐一次，两端不再各说各话
- 目标实体不可用时不再入队、不再重试 5 次，启动不再刷 4 条 WARNING
- 三合一取证拆分：只上报新风的面板不再凭空长出地暖实体
- 灯色温改用 `color_temp_kelvin`（HA 2026 已移除 `LightEntity.color_temp`）

## License

MIT
