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
OP_SCENE_CONTROL: Final = 0x34
OP_TRANSPARENT_CONTROL: Final = 0x40
OP_DEVICE_STATUS_QUERY: Final = 0x32
OP_ENABLE_STATUS_REPORT: Final = 0x10

OP_EVENT_NODE_STATUS: Final = 0x80
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
# Climate mode: mesh -> HA
CLIMATE_MODE_MAP: Final[dict[int, str]] = {1: "cool", 2: "heat", 3: "fan_only", 4: "dry"}
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

# ---------------------------------------------------------------------------
# KNX sync defaults (ported from symi_two_way_sync, local path only)
# ---------------------------------------------------------------------------
CONF_SYNC_NAME: Final = "name"
CONF_SYNC_ENTITIES: Final = "entities"
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
