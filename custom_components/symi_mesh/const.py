"""Constants for the Symi Mesh integration (protocol + entity model)."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "symi_mesh"
VERSION: Final = "1.0.0"

# ---------------------------------------------------------------------------
# Config entry kinds (one domain, two entry flavours)
# ---------------------------------------------------------------------------
KIND_GATEWAY: Final = "gateway"
KIND_KNX_SYNC: Final = "knx_sync"

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
CONF_CONNECTION_TYPE: Final = "connection_type"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_SERIAL_PORT: Final = "serial_port"
CONF_BAUD_RATE: Final = "baud_rate"

CONNECTION_TCP: Final = "tcp"
CONNECTION_SERIAL: Final = "serial"

DEFAULT_PORT: Final = 4196
DEFAULT_BAUD_RATE: Final = 115200
DEFAULT_RECONNECT_INTERVAL: Final = 5  # seconds, base for exponential backoff
MAX_RECONNECT_INTERVAL: Final = 60
DEFAULT_CONNECT_TIMEOUT: Final = 10  # seconds

# ---------------------------------------------------------------------------
# Gateway behaviour
# ---------------------------------------------------------------------------
CONF_RECONNECT_INTERVAL: Final = "reconnect_interval"
CONF_SCENE_BINDINGS: Final = "scene_bindings"  # "entity_id=scene_id" 列表（逗号分隔）
CONF_ENABLE_STATUS_REPORT: Final = "enable_status_report"
CONF_QUERY_ON_STARTUP: Final = "query_on_startup"

# ---------------------------------------------------------------------------
# Wire protocol (ported from node-red-contrib-symi-mesh/lib/protocol.js)
# Frame:  53 [opcode] [len] [payload...] [checksum]           (commands)
#         53 [opcode] [status] [len] [payload...] [checksum]  (events/responses)
# Checksum: XOR of all preceding bytes.
# ---------------------------------------------------------------------------
PROTOCOL_HEADER: Final = 0x53
MIN_FRAME_LENGTH: Final = 4
MAX_FRAME_LENGTH: Final = 1024

OP_READ_SOFTWARE_VERSION: Final = 0x02
OP_READ_DEVICE_LIST: Final = 0x12
OP_DEVICE_CONTROL: Final = 0x30
OP_TRANSPARENT_CONTROL: Final = 0x40
OP_DEVICE_STATUS_QUERY: Final = 0x32
OP_ENABLE_STATUS_REPORT: Final = 0x10
OP_SCENE_CONTROL: Final = 0x31          # 标准场景控制（群控）
OP_SCENE_CONTROL_TRANSPARENT: Final = 0x34  # 透传节点场景执行（广播+重传5次）
OP_READ_NODE_COUNT: Final = 0x15

# 0x80 event status codes that are not node-status data.
EVENT_NODE_ACK: Final = 0x05
EVENT_LIST_NODE_UPDATE_RPT: Final = 0x10

OP_EVENT_NODE_STATUS: Final = 0x80
EVENT_NET_SCENE_ACT: Final = 0x11  # 网络场景执行通知（payload=[addr, scene_no]）
OP_EVENT_TRANSPARENT_MSG: Final = 0xC0
OP_RESP_DEVICE_LIST: Final = 0x92
OP_RESP_DEVICE_CONTROL: Final = 0xB0
OP_RESP_DEVICE_STATUS_QUERY: Final = 0xB2
OP_RESP_SCENE_CONTROL: Final = 0xB4

# Broadcast address used by scene control frames.
SCENE_BROADCAST_ADDR: Final = 0xC0BC

# ---------------------------------------------------------------------------
# Mesh attribute types (msg_type inside 0x30 control / 0x80 events)
# ---------------------------------------------------------------------------
ATTR_SWITCH: Final = 0x02          # 1-4ch switch state (2 bits per channel)
ATTR_BRIGHTNESS: Final = 0x03      # 0-100 %
ATTR_COLOR_TEMP: Final = 0x04      # 0-100 %
ATTR_CURTAIN_RUN_STATUS: Final = 0x05
ATTR_CURTAIN_POSITION: Final = 0x06
ATTR_DOOR: Final = 0x0A
ATTR_MOTION: Final = 0x0C
ATTR_CARD_DET: Final = 0x0E
ATTR_RGB: Final = 0x11             # 5 bytes: R G B WW CW
ATTR_SENSOR_TEMP: Final = 0x16     # 2 bytes LE, temperature * 100
ATTR_SENSOR_HUMI: Final = 0x17     # 1 byte, 0-100 %
ATTR_CLIMATE_TEMP: Final = 0x1B    # 1 byte, 16-30 C
ATTR_CLIMATE_FAN: Final = 0x1C
ATTR_CLIMATE_MODE: Final = 0x1D
ATTR_SWITCH_6_8: Final = 0x45      # 6-8ch switch state, 2 bytes LE
ATTR_FRESH_AIR_SWITCH: Final = 0x68
ATTR_FRESH_AIR_MODE: Final = 0x69
ATTR_FRESH_AIR_SPEED: Final = 0x6A
ATTR_FLOOR_HEATING_SWITCH: Final = 0x6B
ATTR_FLOOR_HEATING_TEMP: Final = 0x6C
ATTR_THREE_IN_ONE: Final = 0x94

# ---------------------------------------------------------------------------
# msg_type -> param length (protocol doc 3.5.1 / 3.5.3). A 0x80 NODE_STATUS
# payload may carry several (msg_type, param) pairs back to back; this table
# splits them. None = variable/unknown length -> stop parsing there.
# ---------------------------------------------------------------------------
MSG_TYPE_PARAM_LEN: Final[dict[int, int | None]] = {
    0x02: 1,   # ON_OFF (2 bits per channel)
    0x03: 1,   # LIGHT_LIGHTNESS
    0x04: 1,   # LIGHT_TEMPERATURE
    0x05: 1,   # CURT_RUN_STATUS
    0x06: 1,   # CURT_RUN_PER_POS
    0x07: 2,   # SCENE_PANEL_EVENT
    0x08: 1,   # CURT_CFG_RUN_DIR
    0x0A: 1,   # DOOR_SENSOR_STATUS
    0x0B: 1,   # BATTERY_LEVEL_STATUS
    0x0C: 1,   # HB_DET_STATUS
    0x0D: 1,   # CTRL_SOURCE
    0x0E: 1,   # CARD_DET_STATUS
    0x0F: 3,   # SOFT_VERSION
    0x10: 1,   # STATUS_CHG_RPT_EN_CFG
    0x11: 5,   # ATTR_RGB: R G B WW CW
    #   原为 1（遗留的 PAIR_FUNC_LOCK_CFG 取值），但 device.py 的 RGB 分支
    #   要求 len(parameters) >= 5，MSG_TYPE_PARAM_LEN 决定了 parse_status_pairs
    #   切分长度 —— 不改会导致 HA 永远收不到完整 RGB（五色调光灯状态不回读）。
    #   全仓核对：0x11 仅被 device.py 当作 ATTR_RGB 使用，无配对锁消费方。
    0x12: 4,   # KEYS_1_4_CFG
    0x13: 2,   # TEMP_SLEEP_MODE
    0x16: 2,   # SENSOR_TEMP (signed, *100)
    0x17: 1,   # SENSOR_HUMI
    0x19: 2,   # KEYS_5_6_CFG
    0x1A: 2,   # PWR_ONOFF_SCENE_CFG
    0x1B: 1,   # TMPC_TEMP
    0x1C: 1,   # TMPC_WIND_SPEED
    0x1D: 1,   # TMPC_MODE
    0x1E: 1,   # TMPC_KB_LOCK
    0x1F: 1,   # TMPC_VALVE
    0x21: 1,   # TMPC_SLEEP
    0x22: 4,   # NAMEID_1_4_CFG
    0x23: 2,   # NAMEID_5_6_CFG
    0x24: 1,   # PWR_ON_AUTO_ONOFF_CFG
    0x25: 1,   # KEYS_ON_MUTEX_CFG
    0x26: 1,   # DEV_LOCK_CFG
    0x27: 3,   # KEY_LOOP_SCENE_CFG
    0x28: 10,  # CURT_SW_RCU_CFG
    0x29: 8,   # DEV_RCU_CFG
    0x2A: 1,   # SENSOR_WATER_ALARM
    0x2B: 1,   # SENSOR_SMOKE_ALARM
    0x2C: 1,   # LOW_BATTERY_LEVEL_ALARM
    0x2D: 1,   # TAMPER_ALARM
    0x2E: 4,   # SCENE_DELAY_ACT_CFG
    0x2F: 1,   # LIGHT_LIGHTNESS_INC
    0x30: 1,   # LIGHT_LIGHTNESS_DEC
    0x31: 1,   # LIGHT_TEMPERATURE_INC
    0x32: 1,   # LIGHT_TEMPERATURE_DEC
    0x33: 1,   # TMPC_TEMP_INC
    0x34: 1,   # TMPC_TEMP_DEC
    0x35: 1,   # TMPC_WIND_SPEED_INC
    0x36: 1,   # TMPC_WIND_SPEED_DEC
    0x68: 1,   # FRESH_AIR_SWITCH
    0x69: 1,   # FRESH_AIR_MODE
    0x6A: 1,   # FRESH_AIR_SPEED
    0x6B: 1,   # FLOOR_HEATING_SWITCH
    0x6C: 1,   # FLOOR_HEATING_TEMP
    0x8F: 2,   # IR_ACTION
    0x94: 8,   # 三合一组合状态
}

# ---------------------------------------------------------------------------
# Device types (from docs/ENTITY_REFERENCE_TABLE.md)
# ---------------------------------------------------------------------------
DEVICE_TYPE_UNKNOWN: Final = 0
DEVICE_TYPE_ZERO_FIRE_SWITCH: Final = 1   # 零火开关 1-6ch
DEVICE_TYPE_SINGLE_FIRE_SWITCH: Final = 2  # 单火开关 1-6ch
DEVICE_TYPE_PLUG: Final = 3               # 智能插座
DEVICE_TYPE_CCT_LIGHT: Final = 4          # 双色调光灯
DEVICE_TYPE_CURTAIN: Final = 5            # 智能窗帘
DEVICE_TYPE_SCENE_PANEL: Final = 6        # 情景面板
DEVICE_TYPE_DOOR_SENSOR: Final = 7        # 门磁
DEVICE_TYPE_MOTION_SENSOR: Final = 8      # 人体感应
DEVICE_TYPE_CARD_SWITCH: Final = 9        # 插卡取电
DEVICE_TYPE_THERMOSTAT: Final = 10        # 温控器
DEVICE_TYPE_TEMP_HUMI_SENSOR: Final = 11  # 温湿度传感器
DEVICE_TYPE_SCENE_SWITCH: Final = 12      # 情景开关
DEVICE_TYPE_TRANSPARENT_SLAVE: Final = 0x14  # 透传丛机
DEVICE_TYPE_RGB_LIGHT: Final = 0x18       # 五色调光灯
DEVICE_TYPE_IO_8: Final = 0x27            # 四输入八输出
DEVICE_TYPE_THREE_IN_ONE: Final = 0x94    # 三合一面板

DEVICE_TYPE_NAMES: Final[dict[int, str]] = {
    0: "未知设备",
    1: "零火开关",
    2: "单火开关",
    3: "智能插座",
    4: "双色调光灯",
    5: "智能窗帘",
    6: "情景面板",
    7: "门磁传感器",
    8: "人体感应器",
    9: "插卡取电器",
    10: "温控器",
    11: "温湿度传感器",
    12: "情景开关",
    13: "离线语控节点",
    14: "智能门锁",
    15: "水浸传感器",
    16: "烟雾报警器",
    17: "智能电视盒子",
    18: "单火情景开关",
    0x14: "透传丛机",
    0x18: "五色调光灯",
    0x27: "四输入八输出",
    0x94: "三合一面板",
}

DEVICE_TYPE_MODELS: Final[dict[int, str]] = {
    1: "Symi 零火开关",
    2: "Symi 单火开关",
    3: "Symi 智能插座",
    4: "Symi 双色调光灯",
    5: "Symi 智能窗帘",
    6: "Symi 情景面板",
    7: "Symi 门磁传感器",
    8: "Symi 人体感应器",
    9: "Symi 插卡取电器",
    10: "Symi 温控器",
    11: "Symi 温湿度传感器",
    12: "Symi 情景开关",
    13: "Symi 离线语控节点",
    14: "Symi 智能门锁",
    15: "Symi 水浸传感器",
    16: "Symi 烟雾报警器",
    17: "Symi 智能电视盒子",
    18: "Symi 单火情景开关",
    0x14: "Symi 透传丛机",
    0x18: "Symi 五色调光灯",
    0x27: "Symi 四输入八输出",
    0x94: "Symi 三合一面板",
}

# Device types whose sub type is the channel count.
MULTI_CHANNEL_TYPES: Final[frozenset[int]] = frozenset({1, 2, 3, 9, 12, 0x27})

# ---------------------------------------------------------------------------
# Entity-domain mapping (mirrors DeviceInfo.getEntityType in device-manager.js)
# ---------------------------------------------------------------------------
DEVICE_TYPE_TO_PLATFORM: Final[dict[int, str]] = {
    1: "switch",
    2: "switch",
    3: "switch",
    4: "light",
    5: "cover",
    6: "scene",
    7: "binary_sensor",
    8: "binary_sensor",
    9: "switch",
    10: "climate",
    11: "sensor",
    12: "scene",
    13: "sensor",     # 离线语控节点：diagnostic sensor
    14: "lock",       # 智能门锁
    15: "binary_sensor",  # 水浸
    16: "binary_sensor",  # 烟雾
    17: "sensor",     # 电视盒子：diagnostic sensor
    18: "switch",     # 单火情景开关（带负载路）
    0x14: "scene",
    0x18: "light",
    0x27: "switch",
    0x94: "climate",  # three-in-one: split into climate + fan + climate
}

# Device types that only report a scene trigger (event-only panels).
SCENE_TRIGGER_TYPES: Final[frozenset[int]] = frozenset({6, 12, 0x14})

# ---------------------------------------------------------------------------
# Value maps
# ---------------------------------------------------------------------------
# Climate mode: mesh -> HA (0x1D: 1=cool 2=heat 3=fan 4=dry; 5=auto from 0x94 struct)
CLIMATE_MODE_MAP: Final[dict[int, str]] = {
    1: "cool",
    2: "heat",
    3: "fan_only",
    4: "dry",
    5: "auto",
}
CLIMATE_MODE_REVERSE: Final[dict[str, int]] = {v: k for k, v in CLIMATE_MODE_MAP.items()}

# Fan mode: mesh -> HA (1=high, 2=medium, 3=low, 4=auto)
FAN_MODE_MAP: Final[dict[int, str]] = {1: "high", 2: "medium", 3: "low", 4: "auto"}
FAN_MODE_REVERSE: Final[dict[str, int]] = {v: k for k, v in FAN_MODE_MAP.items()}

# Curtain action: mesh -> HA
CURTAIN_ACTION_MAP: Final[dict[int, str]] = {1: "opening", 2: "closing", 3: "stopped"}

# MQTT colour-temp range kept for the light platform (mireds).
MIN_MIREDS: Final = 153
MAX_MIREDS: Final = 500

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
SERVICE_SCAN_DEVICES: Final = "scan_devices"
SERVICE_RELOAD: Final = "reload"
SERVICE_DESCRIBE: Final = "describe"
SERVICE_TRIGGER_SCENE: Final = "trigger_scene"

# ---------------------------------------------------------------------------
# KNX sync defaults (ported from symi_two_way_sync, local path only)
# ---------------------------------------------------------------------------
CONF_SYNC_NAME: Final = "name"
CONF_SYNC_ENTITIES: Final = "entities"
# 表单层字段（仅 config flow 用，提交时合并进 entities）：按平台分组快速筛选
CONF_SYNC_MESH_ENTITIES: Final = "mesh_entities"
CONF_SYNC_KNX_ENTITIES: Final = "knx_entities"
CONF_SYNC_OTHER_ENTITIES: Final = "other_entities"
CONF_SYNC_DIRECTION: Final = "direction"
CONF_SYNC_ENABLED: Final = "enabled"
CONF_SYNC_DEBOUNCE_MS: Final = "debounce_ms"
CONF_SYNC_OUTBOUND_MS: Final = "outbound_ms"

SYNC_DIRECTION_TWO_WAY: Final = "two_way"
SYNC_DIRECTION_ONE_WAY: Final = "one_way"

DEFAULT_SYNC_DEBOUNCE_MS: Final = 100
DEFAULT_SYNC_OUTBOUND_MS: Final = 50
DEFAULT_ECHO_TTL_S: Final = 5.0
DEFAULT_AUTHORITY_HOLD_S: Final = 4.0
DEFAULT_FOLLOWER_QUIET_S: Final = 3.0
DEFAULT_GROUP_LATCH_S: Final = 4.0

# 死循环熔断：真正的死循环是毫秒级自动乒乓（A→B→A…），用短窗口高频检测，
# 避免把「用户反复手动操作」或「对端不可用抖动」误判为死循环而长时间熔断。
LOOP_WINDOW_S: Final = 2.0
LOOP_BREAKER_LIMIT: Final = 8
LOOP_COOLDOWN_S: Final = 60.0
EVENT_LOOP_BREAKER: Final = f"{DOMAIN}_loop_breaker"
SYNC_QUEUE_MAX_SIZE: Final = 1024
SYNC_WORKER_CONCURRENCY: Final = 4
SYNC_MAX_CONCURRENT_APPLIES: Final = 8

# Domains that may take part in a sync group (KNX + common counterparts).
SYNCABLE_DOMAINS: Final[tuple[str, ...]] = (
    "light",
    "switch",
    "cover",
    "fan",
    "climate",
    "lock",
    "media_player",
    "number",
    "input_number",
    "select",
    "input_select",
    "input_boolean",
    "input_text",
    "text",
    "humidifier",
    "water_heater",
    "vacuum",
    "scene",
    "script",
    "automation",
    "valve",
    "siren",
    "button",
)

TOGGLE_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "light",
        "switch",
        "fan",
        "input_boolean",
        "automation",
        "script",
        "siren",
        "humidifier",
        "media_player",
        "vacuum",
        "water_heater",
        "climate",
        "cover",
        "valve",
    }
)

LIGHT_ATTRIBUTES: Final[tuple[str, ...]] = (
    "brightness",
    "color_temp_kelvin",
    "color_temp",
    "rgb_color",
    "hs_color",
    "xy_color",
    "effect",
)

COVER_ATTRIBUTES: Final[tuple[str, ...]] = (
    "current_cover_position",
    "current_cover_tilt_position",
)

FAN_ATTRIBUTES: Final[tuple[str, ...]] = (
    "percentage",
    "preset_mode",
    "oscillating",
    "direction",
)

CLIMATE_ATTRIBUTES: Final[tuple[str, ...]] = (
    "temperature",
    "target_temp_high",
    "target_temp_low",
    "hvac_mode",
    "fan_mode",
    "swing_mode",
    "humidity",
)

EVENT_SYNC_APPLIED: Final = f"{DOMAIN}_sync_applied"

# ---------------------------------------------------------------------------
# Engine-facing names (knx_sync.py imports these exact identifiers).
# ---------------------------------------------------------------------------
CONF_NAME: Final = CONF_SYNC_NAME
CONF_ENTITIES: Final = CONF_SYNC_ENTITIES
CONF_DIRECTION: Final = CONF_SYNC_DIRECTION
DIRECTION_TWO_WAY: Final = SYNC_DIRECTION_TWO_WAY
DIRECTION_ONE_WAY: Final = SYNC_DIRECTION_ONE_WAY

AUTHORITY_HOLD_S: Final = DEFAULT_AUTHORITY_HOLD_S
FOLLOWER_QUIET_S: Final = DEFAULT_FOLLOWER_QUIET_S
GROUP_LATCH_S: Final = DEFAULT_GROUP_LATCH_S
CLOUD_WRITE_CONCURRENCY: Final = 2
DEFAULT_RETRY_ATTEMPTS: Final = 5
DEFAULT_RETRY_BASE_DELAY_S: Final = 0.4
MAX_CONCURRENT_APPLIES: Final = SYNC_MAX_CONCURRENT_APPLIES
QUEUE_MAX_SIZE: Final = SYNC_QUEUE_MAX_SIZE
UNAVAILABLE_LOG_COOLDOWN_S: Final = 120.0
WORKER_CONCURRENCY: Final = SYNC_WORKER_CONCURRENCY
