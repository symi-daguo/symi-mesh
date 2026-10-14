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
| 串口 | 设备路径 + 波特率（默认 115200） | HA OS 需先将串口映射进容器（如 `core-serial` / `Ser2net` 插件或 `devices:` 配置） |

连接后自动完成：读取设备列表 → 为每台设备创建实体 → 开启状态上报（0x10）→ 首次全量状态查询（识别三合一面板）。

## 支持的设备

| Mesh 设备类型 | HA 实体 | 说明 |
|--------------|---------|------|
| 零火/单火开关（1-8 路） | `switch` × 路数 | 每路独立实体 |
| 智能插座 | `switch` | |
| 插卡取电 | `switch` + `binary_sensor` | 插卡检测 |
| 双色调光灯 | `light` | 亮度 + 色温 |
| 五色调光灯 | `light` | 亮度 + 色温 + RGB |
| 智能窗帘 | `cover` | 开/关/停/位置 |
| 温控器 | `climate` | 温度/模式/风速 |
| 温湿度传感器 | `sensor` × 2 | 温度 + 湿度 |
| 门磁 / 人体感应 | `binary_sensor` | |
| 情景面板/开关 | `scene` | |
| 三合一面板 | `climate` + `fan` + `climate` | 空调 + 新风 + 地暖，运行时自动识别 |

所有设备通过 `via_device` 挂到网关设备下；网关断线时全部实体自动变 `不可用`，恢复后自动重连（指数退避，最长 60s）。

## KNX 双向同步

添加集成 → 选择「KNX 双向同步」→ 选择 **≥2 个实体**（例如 `knx.switch.xxx` 与本集成的 `switch.xxx`）：

- **防回环**：每次写入前打 origin 戳记，回显事件按签名消费；主控权威 + 从端静默 + 组意图闩锁三重保护
- **限速**：本地实体默认防抖 100ms / 出队 50ms；云端实体（米家等）自动放慢
- **方向**：双向（任意一端驱动其余）或单向（仅第一个驱动其余）
- 断电/重启后自动恢复；修改配置即时生效

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
