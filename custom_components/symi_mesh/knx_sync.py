"""Sync engine: queue + debounce + origin-stamped echo guard.

Anti-loop contract:
1. Before every outbound service call, stamp the intended payload signature
   into a per-entity echo map.
2. On state_changed, drop the event if its payload signature matches a
   recent stamp (and consume it). Time-window-only guards are not used.
3. Skip enqueue when the target already matches the desired payload.
4. Per-entity write lock serializes applies to the same target.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

from .const import (
    AUTHORITY_HOLD_S,
    CLIMATE_ATTRIBUTES,
    CLOUD_WRITE_CONCURRENCY,
    CONF_DIRECTION,
    CONF_ENTITIES,
    CONF_NAME,
    COVER_ATTRIBUTES,
    DEFAULT_ECHO_TTL_S,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_BASE_DELAY_S,
    DIRECTION_ONE_WAY,
    DIRECTION_TWO_WAY,
    DOMAIN,
    EVENT_LOOP_BREAKER,
    EVENT_SYNC_APPLIED,
    FAN_ATTRIBUTES,
    FOLLOWER_QUIET_S,
    GROUP_LATCH_S,
    LOOP_BREAKER_LIMIT,
    LOOP_COOLDOWN_S,
    LOOP_WINDOW_S,
    MAX_CONCURRENT_APPLIES,
    QUEUE_MAX_SIZE,
    SYNCABLE_DOMAINS,
    TOGGLE_DOMAINS,
    UNAVAILABLE_LOG_COOLDOWN_S,
    WORKER_CONCURRENCY,
)
from .exceptions import ApplyFailedError, EntityUnavailableError
from .path_kind import entity_path_kind
from .settings import debounce_s, outbound_interval_s

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

_ON_STATES = frozenset({"on", "open", "opening", "playing", "home", "unlocked", "heat", "cool", "auto", "dry", "fan_only"})
_OFF_STATES = frozenset({"off", "closed", "closing", "idle", "locked", "standby", "paused"})


def entity_kind(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _is_on(state: str) -> bool | None:
    if state in _ON_STATES:
        return True
    if state in _OFF_STATES:
        return False
    return None


def extract_payload(state: "State", kind: str) -> dict[str, Any]:
    """Build the sync payload from a HA state."""
    attrs = state.attributes or {}
    payload: dict[str, Any] = {"state": state.state}

    if kind == "light":
        if attrs.get("brightness") is not None:
            try:
                payload["brightness"] = int(round(float(attrs["brightness"])))
            except (TypeError, ValueError):
                pass
        if attrs.get("effect") is not None:
            payload["effect"] = attrs["effect"]
        # Keep only the active color mode to avoid HA Color descriptors conflict.
        mode = attrs.get("color_mode")
        if mode == "color_temp":
            if attrs.get("color_temp_kelvin") is not None:
                payload["color_temp_kelvin"] = int(attrs["color_temp_kelvin"])
            elif attrs.get("color_temp") is not None:
                payload["color_temp"] = attrs["color_temp"]
        elif mode == "rgb" and attrs.get("rgb_color") is not None:
            rgb = _normalize_rgb(attrs["rgb_color"])
            if rgb is not None:
                payload["rgb_color"] = rgb
        elif mode == "hs" and attrs.get("hs_color") is not None:
            hs = _normalize_hs(attrs["hs_color"])
            if hs is not None:
                payload["hs_color"] = hs
        elif mode == "xy" and attrs.get("xy_color") is not None:
            payload["xy_color"] = tuple(attrs["xy_color"])
        elif mode in (None, "brightness", "onoff"):
            # No color channel — brightness-only / on-off lights.
            pass
        else:
            # Fallback when color_mode is unexpected but a color attr exists.
            if attrs.get("rgb_color") is not None:
                rgb = _normalize_rgb(attrs["rgb_color"])
                if rgb is not None:
                    payload["rgb_color"] = rgb
            elif attrs.get("hs_color") is not None:
                hs = _normalize_hs(attrs["hs_color"])
                if hs is not None:
                    payload["hs_color"] = hs
            elif attrs.get("color_temp_kelvin") is not None:
                payload["color_temp_kelvin"] = int(attrs["color_temp_kelvin"])
            elif attrs.get("color_temp") is not None:
                payload["color_temp"] = attrs["color_temp"]
            elif attrs.get("xy_color") is not None:
                payload["xy_color"] = tuple(attrs["xy_color"])
    elif kind == "cover":
        for key in COVER_ATTRIBUTES:
            if key in attrs and attrs[key] is not None:
                payload[key] = attrs[key]
    elif kind == "fan":
        for key in FAN_ATTRIBUTES:
            if key in attrs and attrs[key] is not None:
                payload[key] = attrs[key]
    elif kind == "climate":
        for key in CLIMATE_ATTRIBUTES:
            if key in attrs and attrs[key] is not None:
                payload[key] = attrs[key]
        if "temperature" not in payload and attrs.get("temperature") is not None:
            payload["temperature"] = attrs["temperature"]
    elif kind in ("number", "input_number"):
        try:
            payload["value"] = float(state.state)
        except (TypeError, ValueError):
            pass
    elif kind in ("select", "input_select"):
        payload["option"] = state.state
    elif kind in ("input_text", "text"):
        payload["value"] = state.state
    elif kind == "button":
        # Momentary — only used as a fire-once trigger to peer button/scene/script.
        payload["pressed"] = True
    elif kind == "humidifier":
        if "humidity" in attrs:
            payload["humidity"] = attrs["humidity"]
        if "mode" in attrs:
            payload["mode"] = attrs["mode"]
    elif kind == "water_heater":
        if "temperature" in attrs:
            payload["temperature"] = attrs["temperature"]
        if "operation_mode" in attrs:
            payload["operation_mode"] = attrs["operation_mode"]
    elif kind == "media_player":
        if "volume_level" in attrs:
            payload["volume_level"] = attrs["volume_level"]
        if "is_volume_muted" in attrs:
            payload["is_volume_muted"] = attrs["is_volume_muted"]
    elif kind == "lock":
        pass
    elif kind == "valve":
        if "current_position" in attrs:
            payload["current_position"] = attrs["current_position"]

    return payload


# ---------------------------------------------------------------------------
# Cross-vendor value vocabularies
# ---------------------------------------------------------------------------
# 同一台三合一设备，mesh 侧风速是 low/medium/high/auto，KNX 侧取决于 ETS 的
# DPT 与 fan_max_step：off/low/medium/high、0%/33%/66%/100% 或 0/1/2/3。
# 不归一化就会互相写不认的值 —— HA 服务调用直接抛错，重试 5 次后打 ERROR，
# 且对端回读仍是老值，形成「写入失败 → 再判差异 → 再写」的慢性循环。
_FAN_LEVEL_ALIASES: dict[str, str] = {
    "off": "off",
    "0": "off",
    "0%": "off",
    "none": "off",
    "low": "low",
    "1": "low",
    "33": "low",
    "33%": "low",
    "medium": "medium",
    "2": "medium",
    "50%": "medium",
    "66": "medium",
    "66%": "medium",
    "67": "medium",
    "high": "high",
    "3": "high",
    "100": "high",
    "100%": "high",
    "on": "high",
    "auto": "auto",
    "automatic": "auto",
}
_LEVEL_PERCENTAGE: dict[str, int] = {"low": 33, "medium": 66, "high": 100}
_HVAC_ALIASES: dict[str, str] = {
    "off": "off",
    "heat": "heat",
    "heating": "heat",
    "cool": "cool",
    "cooling": "cool",
    "fan_only": "fan_only",
    "fan": "fan_only",
    "dry": "dry",
    "dehumidification": "dry",
    "auto": "auto",
    "heat_cool": "auto",
}


def _fan_level(value: Any) -> str | None:
    if value is None:
        return None
    return _FAN_LEVEL_ALIASES.get(str(value).strip().lower())


def normalize_fan_mode(value: Any, target_modes: list[str] | None) -> str | None:
    """Map a fan-mode string onto the peer's declared mode list.

    Returns ``None`` when the peer cannot express this speed at all — the
    caller must then drop the attribute instead of sending an invalid value.
    """
    level = _fan_level(value)
    if level is None:
        return None
    if not target_modes:
        return level
    if str(value) in target_modes:
        return str(value)
    for candidate in target_modes:
        if _fan_level(candidate) == level:
            return candidate
    return None


def quantize_percentage(value: Any, speed_count: int | None) -> int | None:
    """Snap a percentage to the peer's discrete steps (3 speeds → 33/66/100)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return 0
    if not speed_count or speed_count < 2:
        return int(round(number))
    step = 100.0 / speed_count
    index = max(1, min(speed_count, round(number / step)))
    # 截断而非四舍五入：3 档要落回 mesh 真实档位 33/66/100，
    # round(66.67)=67 会让 mesh 侧每次都要用容差判等，日志里全是近似匹配。
    return int(index * 100 / speed_count)


def _adapt_fan_payload(
    payload: dict[str, Any], target_state: "State | None"
) -> dict[str, Any]:
    """Fan → fan: quantise percentage, drop preset the peer cannot express."""
    attrs = (target_state.attributes if target_state is not None else {}) or {}
    preset_modes = list(attrs.get("preset_modes") or [])
    speed_count = attrs.get("speed_count")
    out = dict(payload)

    preset = out.get("preset_mode")
    if preset is not None:
        if preset in preset_modes:
            out["preset_mode"] = preset
            # 有 preset 时不再叠加 percentage，避免 HA 侧两个速度源打架。
            out.pop("percentage", None)
        else:
            level = _fan_level(preset)
            out.pop("preset_mode", None)
            if level == "off":
                out["state"] = "off"
                out.pop("percentage", None)
            else:
                # KNX fan 没有 preset 概念：auto 只能落到最高档
                # （README 的「五条硬约束」第 5 条已写明）。
                percentage = 100 if level == "auto" else _LEVEL_PERCENTAGE.get(level or "")
                if percentage is None:
                    out.pop("percentage", None)
                else:
                    out["percentage"] = percentage

    if "percentage" in out:
        target_sc = int(speed_count) if isinstance(speed_count, (int, float)) else (
            3 if preset_modes else None
        )
        quantized = quantize_percentage(out["percentage"], target_sc)
        if quantized is None:
            out.pop("percentage", None)
        elif quantized <= 0:
            out["state"] = "off"
            out.pop("percentage", None)
        else:
            out["percentage"] = quantized

    if target_state is not None:
        for key in ("percentage", "preset_mode"):
            if key in out and key in attrs and _value_close(key, out[key], attrs[key]):
                out.pop(key)
        if set(out) <= {"state"} and str(out.get("state")) == str(target_state.state):
            return None
    return out


def _adapt_climate_payload(
    payload: dict[str, Any], target_state: "State | None"
) -> dict[str, Any]:
    """Climate → climate: normalise fan_mode / hvac_mode, clamp setpoint."""
    attrs = (target_state.attributes if target_state is not None else {}) or {}
    fan_modes = list(attrs.get("fan_modes") or [])
    hvac_modes = [str(m) for m in (attrs.get("hvac_modes") or [])]
    out = dict(payload)

    if "fan_mode" in out:
        normalized = normalize_fan_mode(out["fan_mode"], fan_modes)
        if normalized is None:
            out.pop("fan_mode", None)
        else:
            out["fan_mode"] = normalized

    if "hvac_mode" in out and hvac_modes:
        mode = str(out["hvac_mode"])
        if mode not in hvac_modes:
            alias = _HVAC_ALIASES.get(mode.lower())
            if alias is not None and alias in hvac_modes:
                out["hvac_mode"] = alias
            else:
                out.pop("hvac_mode", None)

    for key in ("temperature", "target_temp_low", "target_temp_high"):
        if key not in out:
            continue
        low, high = attrs.get("min_temp"), attrs.get("max_temp")
        try:
            value = float(out[key])
            if isinstance(low, (int, float)) and value < float(low):
                out[key] = float(low)
            if isinstance(high, (int, float)) and value > float(high):
                out[key] = float(high)
        except (TypeError, ValueError):
            out.pop(key, None)

    if target_state is not None:
        # 只发真正变了的字段：改风速不该把温度也重发一遍（KNX 总线带宽有限，
        # 多余报文还会让 ETS 组监控里出现"没人操作却在写"的假象）。
        for key in ("temperature", "target_temp_low", "target_temp_high",
                    "hvac_mode", "fan_mode", "swing_mode", "humidity"):
            if key in out and key in attrs and _value_close(key, out[key], attrs[key]):
                out.pop(key)
        if set(out) <= {"state"} and str(out.get("state")) == str(target_state.state):
            return None
    return out


def convert_payload(
    payload: dict[str, Any],
    source_kind: str,
    target_kind: str,
    target_state: "State | None" = None,
) -> dict[str, Any] | None:
    """Adapt a source payload for a (possibly different) target domain.

    ``target_state`` carries the peer's declared capabilities (``fan_modes``,
    ``preset_modes``, ``hvac_modes``, ``speed_count``).  KNX and mesh describe
    the same physical fan speed with different vocabularies ("33%"/"1"/
    "low"), so every cross-vendor value is normalised here — never at the
    service-call layer, where an unknown value becomes an error and a retry.
    """
    if source_kind == target_kind:
        out = dict(payload)
        if source_kind == "climate":
            return _adapt_climate_payload(out, target_state)
        if source_kind == "fan":
            return _adapt_fan_payload(out, target_state)
        return out

    # Same-family numeric helpers
    if source_kind in ("number", "input_number") and target_kind in (
        "number",
        "input_number",
    ):
        return {"state": payload.get("state"), "value": payload.get("value", payload.get("state"))}
    if source_kind in ("select", "input_select") and target_kind in (
        "select",
        "input_select",
    ):
        return {"state": payload.get("state"), "option": payload.get("option", payload.get("state"))}

    if source_kind in ("input_text", "text") and target_kind in ("input_text", "text"):
        return {"state": payload.get("state"), "value": payload.get("value", payload.get("state"))}

    # Climate ↔ climate already handled by same-domain. Climate ↔ number: setpoint.
    if source_kind == "climate" and target_kind in ("number", "input_number"):
        temp = payload.get("temperature")
        if temp is None:
            return None
        return {"state": str(temp), "value": float(temp)}
    if source_kind in ("number", "input_number") and target_kind == "climate":
        try:
            temp = float(payload.get("value", payload.get("state")))
        except (TypeError, ValueError):
            return None
        return {"state": "heat", "temperature": temp, "hvac_mode": "heat"}

    # Scene / button / script fire-once peers
    if source_kind in ("scene", "button", "script") and target_kind in (
        "scene",
        "button",
        "script",
    ):
        return {"state": "on", "pressed": True}

    # Cross-domain: only binary/on-off when both sides are toggleable.
    if source_kind in TOGGLE_DOMAINS and target_kind in TOGGLE_DOMAINS:
        flag = _is_on(str(payload.get("state", "")))
        if flag is None:
            return None
        return {"state": "on" if flag else "off"}

    # cover <-> valve position-ish
    if {source_kind, target_kind} <= {"cover", "valve"}:
        out = {"state": payload.get("state")}
        pos = payload.get("current_cover_position", payload.get("current_position"))
        if pos is not None:
            out["current_position" if target_kind == "valve" else "current_cover_position"] = pos
        return out

    # lock <-> switch/input_boolean
    if source_kind == "lock" and target_kind in TOGGLE_DOMAINS:
        return {"state": "off" if payload.get("state") == "locked" else "on"}
    if target_kind == "lock" and source_kind in TOGGLE_DOMAINS:
        return {"state": "unlocked" if payload.get("state") == "on" else "locked"}

    return None


def _normalize_rgb(value: Any) -> tuple[int, int, int] | None:
    try:
        r, g, b = value
        return (int(round(float(r))), int(round(float(g))), int(round(float(b))))
    except (TypeError, ValueError):
        return None


def _normalize_hs(value: Any) -> tuple[float, float] | None:
    try:
        h, s = value
        return (round(float(h), 1), round(float(s), 1))
    except (TypeError, ValueError):
        return None


def _nums_close(a: Any, b: Any, tol: float) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def light_payloads_close(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when two light payloads are effectively the same (device rounding)."""
    if a.get("state") != b.get("state"):
        return False
    if "brightness" in a or "brightness" in b:
        if not _nums_close(a.get("brightness", 0), b.get("brightness", 0), 3):
            return False
    if "rgb_color" in a or "rgb_color" in b:
        ra, rb = a.get("rgb_color"), b.get("rgb_color")
        if ra is None or rb is None:
            return False
        try:
            return all(abs(int(x) - int(y)) <= 5 for x, y in zip(ra, rb, strict=True))
        except (TypeError, ValueError):
            return False
    if "hs_color" in a or "hs_color" in b:
        ha, hb = a.get("hs_color"), b.get("hs_color")
        if ha is None or hb is None:
            return False
        return _nums_close(ha[0], hb[0], 3) and _nums_close(ha[1], hb[1], 3)
    if "color_temp_kelvin" in a or "color_temp_kelvin" in b:
        return _nums_close(
            a.get("color_temp_kelvin"), b.get("color_temp_kelvin"), 50
        )
    if "color_temp" in a or "color_temp" in b:
        return _nums_close(a.get("color_temp"), b.get("color_temp"), 5)
    return True


# 跨协议数值抖动容差：KNX 的 66% 与 mesh 的 66.67 四舍五入后差 1，
# 空调设定值来回换算也可能差 0.1。严格相等会把「同一个值」判成差异，
# 于是每轮都重新下发一次 —— 这就是慢性乒乓的来源。
_VALUE_TOLERANCE: dict[str, float] = {
    "brightness": 3.0,
    "percentage": 3.0,
    "temperature": 0.2,
    "target_temp_low": 0.2,
    "target_temp_high": 0.2,
    "current_temperature": 0.2,
    "humidity": 1.0,
    "volume_level": 0.02,
}


def _value_close(key: str, a: Any, b: Any) -> bool:
    tolerance = _VALUE_TOLERANCE.get(key)
    if tolerance is None:
        return a == b
    try:
        return abs(float(a) - float(b)) <= tolerance
    except (TypeError, ValueError):
        return a == b


def payloads_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    # Light-aware approximate compare when either side carries color attrs.
    light_keys = {
        "brightness",
        "rgb_color",
        "hs_color",
        "xy_color",
        "color_temp",
        "color_temp_kelvin",
        "effect",
    }
    if light_keys & (set(a) | set(b)):
        return light_payloads_close(a, b)
    if set(a) != set(b):
        keys = set(a) & set(b)
        if "state" not in keys:
            return False
        return all(_value_close(k, a[k], b[k]) for k in keys)
    return all(_value_close(k, a[k], b[k]) for k in a)


def payload_signature(payload: dict[str, Any]) -> str:
    return repr(sorted(payload.items()))


class _Debouncer:
    """Thread-safe debouncer that always arms timers on the HA event loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        delay_s: float,
        on_fire: Callable[[], None],
    ) -> None:
        self._loop = loop
        self._delay_s = max(delay_s, 0.0)
        self._on_fire = on_fire
        self._handle: asyncio.TimerHandle | None = None
        self._pending = False

    def trigger(self) -> None:
        self._pending = True
        self._loop.call_soon_threadsafe(self._arm)

    def _arm(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if not self._pending:
            return
        if self._delay_s <= 0:
            self._fire()
            return
        self._handle = self._loop.call_later(self._delay_s, self._fire)

    def _fire(self) -> None:
        self._handle = None
        if not self._pending:
            return
        self._pending = False
        try:
            self._on_fire()
        except Exception:  # pragma: no cover
            _LOGGER.exception("防抖回调异常")

    def cancel(self) -> None:
        self._pending = False
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        try:
            self._loop.call_soon_threadsafe(lambda: None)
        except RuntimeError:
            pass


class SymiSyncEngine:
    """One engine instance per config entry (one sync group)."""

    def __init__(self, hass: "HomeAssistant", group: dict[str, Any]) -> None:
        self.hass = hass
        self._group = group
        self._unsub: list[Callable[[], None]] = []
        self._debouncers: dict[str, _Debouncer] = {}
        self._entity_locks: dict[str, asyncio.Lock] = {}
        self._echo_map: dict[str, dict[str, float]] = {}
        self._echo_expected: dict[str, list[tuple[dict[str, Any], float]]] = {}
        # Master that the user just changed — refuse peer write-backs.
        self._authority_until: dict[str, float] = {}
        # Follower we just wrote — ignore its state_changed as a sync source.
        self._quiet_until: dict[str, float] = {}
        self._last_master_payload: dict[str, dict[str, Any]] = {}
        self._last_written: dict[str, dict[str, Any]] = {}
        # Group latch: desired on/off while cloud/bus settles — blocks reverse.
        self._group_desired: dict[str, Any] | None = None
        self._group_desired_until: float = 0.0
        self._group_master: str | None = None
        self._quiet_handles: dict[str, asyncio.TimerHandle] = {}
        self._seq = 0
        self._last_outbound_ts: dict[str, float] = {}
        self._unavailable_log_ts: dict[str, float] = {}
        self._queue: asyncio.Queue[
            tuple[str, dict[str, Any], str, str, int]
        ] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._workers: list[asyncio.Task[None]] = []
        self._inflight_sem = asyncio.Semaphore(MAX_CONCURRENT_APPLIES)
        self._stopping = False
        # 死循环熔断：每实体写入时间戳环形窗口 + 熔断恢复时间表
        self._write_times: dict[str, deque[float]] = {}
        self._loop_disabled: dict[str, float] = {}
        # 启动对齐：HA 重启后两端各自恢复旧值，不主动对齐就会一直不一致，
        # 直到有人手动操作。以组内第一个实体首次出现的有效状态为准推一次。
        self._aligned = False

    @property
    def group(self) -> dict[str, Any]:
        return self._group

    def update_group(self, group: dict[str, Any]) -> None:
        self._group = group

    async def async_start(self) -> None:
        self._stopping = False
        self._aligned = False
        self._install_listeners()
        for i in range(WORKER_CONCURRENCY):
            # Background tasks must not block HA startup wrap-up.
            create = getattr(self.hass, "async_create_background_task", None)
            if create is not None:
                self._workers.append(
                    create(self._worker_loop(), name=f"symi_sync_worker_{i}")
                )
            else:
                self._workers.append(self.hass.async_create_task(self._worker_loop()))

    async def async_stop(self) -> None:
        self._stopping = True
        for w in self._workers:
            w.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # Drain queue so pending items don't linger
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self._teardown_listeners()
        for d in self._debouncers.values():
            d.cancel()
        self._debouncers.clear()
        self._entity_locks.clear()
        self._echo_map.clear()
        self._echo_expected.clear()
        self._authority_until.clear()
        self._quiet_until.clear()
        self._last_master_payload.clear()
        self._last_written.clear()
        for h in self._quiet_handles.values():
            h.cancel()
        self._quiet_handles.clear()
        self._group_desired = None
        self._group_desired_until = 0.0
        self._group_master = None
        self._unavailable_log_ts.clear()
        self._write_times.clear()
        self._loop_disabled.clear()

    def _states_conflict(self, a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
        """True when both have an on/off-like state and they disagree."""
        if not a or not b:
            return False
        sa, sb = a.get("state"), b.get("state")
        if sa in ("on", "off") and sb in ("on", "off"):
            return sa != sb
        return False

    async def async_reload(self, group: dict[str, Any]) -> None:
        await self._teardown_listeners()
        self._group = group
        self._install_listeners()

    def _entity_ids(self) -> list[str]:
        return list(self._group.get(CONF_ENTITIES) or [])

    def _install_listeners(self) -> None:
        from homeassistant.helpers.event import async_track_state_change_event

        ids = self._entity_ids()
        if not ids or not self._group.get("enabled", True):
            return
        self._unsub.append(
            async_track_state_change_event(self.hass, ids, self._on_state_changed)
        )

    async def _teardown_listeners(self) -> None:
        for fn in self._unsub:
            fn()
        self._unsub.clear()

    def _on_state_changed(self, event: "Event[EventStateChangedData]") -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if new_state.state in ("unavailable", "unknown"):
            return

        now = time.monotonic()
        # Our own write echo — never treat as a new master click.
        if self._consume_echo(entity_id, new_state):
            return

        # Follower quiet: ignore only lag echoes of our own write on this
        # entity. A value DIFFERENT from what we wrote is a direct user action
        # on the follower — that must become the new master immediately,
        # otherwise the post-quiet re-push would bounce the user's command.
        if now < self._quiet_until.get(entity_id, 0.0):
            written = self._last_written.get(entity_id)
            payload_now = extract_payload(new_state, entity_kind(entity_id))
            if written is not None and (
                payloads_equal(payload_now, written)
                or payload_now.get("state") == written.get("state")
            ):
                _LOGGER.debug("从端静默忽略（写入确认）: %s state=%s", entity_id, new_state.state)
                return
            # External intent: cancel quiet + pending re-push, fall through to
            # the normal promote path so last-click-wins applies.
            self._quiet_until.pop(entity_id, None)
            handle = self._quiet_handles.pop(entity_id, None)
            if handle is not None:
                handle.cancel()
            _LOGGER.info("静默期检测到外部操作，切换主控: %s", entity_id)

        kind = entity_kind(entity_id)
        payload = extract_payload(new_state, kind)

        # Duplicate confirmation from cloud/local while still master — do not re-queue.
        if now < self._authority_until.get(entity_id, 0.0):
            prev = self._last_master_payload.get(entity_id)
            if prev is not None and (
                payloads_equal(prev, payload)
                or (kind == "light" and light_payloads_close(prev, payload))
            ):
                _LOGGER.debug("主控重复确认忽略: %s", entity_id)
                return

        # 首次拿到组内主实体（第一个）的有效状态时做一次启动对齐。
        if not self._aligned:
            self._async_align_once(entity_id, payload, now)

        # Last click wins on either side — promote immediately and sync out.
        self._promote_master(entity_id, payload, now)

        self._seq += 1
        path = entity_path_kind(self.hass, entity_id)
        total_s = debounce_s(self.hass, path)

        deb = self._debouncers.get(entity_id)
        if deb is None:
            deb = _Debouncer(
                self.hass.loop,
                total_s,
                self._make_dispatch_cb(entity_id),
            )
            self._debouncers[entity_id] = deb
        else:
            deb._delay_s = total_s  # noqa: SLF001 — live retune
        deb.trigger()

    def _async_align_once(
        self, entity_id: str, payload: dict[str, Any], now: float
    ) -> None:
        """首次收敛：组内第一个实体一旦有有效状态，就把它的值推给其余实体。

        HA 重启后 KNX 实体从 restore_state 恢复、mesh 实体等网关首轮上报，
        只装监听不做对齐，两端会各说各话直到有人手动掰一次。这里以「组内
        第一个实体」为权威，方向与单向同步的语义一致，用户想换权威只需在
        配置里调整实体顺序。
        """
        ids = self._entity_ids()
        if not ids or entity_id != ids[0]:
            return
        self._aligned = True
        self._authority_until[entity_id] = now + AUTHORITY_HOLD_S
        self._last_master_payload[entity_id] = payload
        self._group_master = entity_id
        self._group_desired = dict(payload)
        self._group_desired_until = now + GROUP_LATCH_S

        source_kind = entity_kind(entity_id)
        pushed = 0
        for target in ids[1:]:
            target_kind = entity_kind(target)
            converted = convert_payload(
                payload, source_kind, target_kind, self.hass.states.get(target)
            )
            if converted is None:
                continue
            self._seq += 1
            self._enqueue(target, converted, source_kind, target_kind, self._seq)
            pushed += 1
        _LOGGER.info(
            "同步组「%s」启动对齐：以 %s 为准，推送 %d 个实体",
            self._group.get(CONF_NAME),
            entity_id,
            pushed,
        )

    def _promote_master(
        self, entity_id: str, payload: dict[str, Any], now: float | None = None
    ) -> None:
        """Mark entity as group master and latch desired state (anti-bounce)."""
        if now is None:
            now = time.monotonic()
        self._authority_until[entity_id] = now + AUTHORITY_HOLD_S
        self._last_master_payload[entity_id] = payload
        self._group_master = entity_id
        self._group_desired = dict(payload)
        self._group_desired_until = now + GROUP_LATCH_S
        # Drop in-flight opposite writes (e.g. late OFF after user already turned ON).
        self._drop_conflicting_queued(payload)
        # User clicked this side — it is never "quiet follower" anymore.
        self._quiet_until.pop(entity_id, None)
        h = self._quiet_handles.pop(entity_id, None)
        if h is not None:
            h.cancel()
        for peer in self._entity_ids():
            if peer == entity_id:
                continue
            self._authority_until.pop(peer, None)
            self._last_master_payload.pop(peer, None)
            # Cancel pending peer dispatch so a late debounce cannot reverse.
            deb = self._debouncers.get(peer)
            if deb is not None:
                deb.cancel()
            # Drop stale after-quiet from an older write to this peer.
            self._quiet_until.pop(peer, None)
            ph = self._quiet_handles.pop(peer, None)
            if ph is not None:
                ph.cancel()

    def _drop_conflicting_queued(self, desired: dict[str, Any]) -> None:
        """Remove queued applies that would reverse the new master intent."""
        if self._queue.empty():
            return
        kept: list[tuple[str, dict[str, Any], str, str, int]] = []
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            _target, payload, _sk, _tk, _seq = item
            if self._states_conflict(payload, desired):
                dropped += 1
                continue
            kept.append(item)
        for item in kept:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                break
        if dropped:
            _LOGGER.debug("丢弃与新主控冲突的排队写入: %d", dropped)

    async def _after_follower_quiet(self, entity_id: str) -> None:
        """After quiet: re-push master if lagging; never reverse an active latch."""
        self._quiet_handles.pop(entity_id, None)
        if self._stopping:
            return
        now = time.monotonic()
        if now < self._quiet_until.get(entity_id, 0.0) - 0.02:
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return
        kind = entity_kind(entity_id)
        current = extract_payload(state, kind)
        written = self._last_written.get(entity_id)
        if written is not None and (
            payloads_equal(current, written)
            or current.get("state") == written.get("state")
        ):
            return

        # Still under group latch and conflicts with master intent → re-push, no reverse.
        if (
            now < self._group_desired_until
            and self._group_desired is not None
            and self._states_conflict(current, self._group_desired)
        ):
            master = self._group_master
            if master and master != entity_id:
                src_kind = entity_kind(master)
                converted = convert_payload(
                    self._group_desired, src_kind, kind, state
                )
                if converted is not None:
                    _LOGGER.debug(
                        "静默结束仍落后主控，重推从端: %s -> %s",
                        self._group_desired.get("state"),
                        entity_id,
                    )
                    self._seq += 1
                    self._enqueue(
                        entity_id, converted, src_kind, kind, self._seq
                    )
            return

        # Latch expired and follower differs — treat as deferred user click.
        _LOGGER.debug("静默结束从端新状态，升为主控: %s", entity_id)
        self._promote_master(entity_id, current, now)
        self._seq += 1
        path = entity_path_kind(self.hass, entity_id)
        total_s = debounce_s(self.hass, path)
        deb = self._debouncers.get(entity_id)
        if deb is None:
            deb = _Debouncer(
                self.hass.loop,
                total_s,
                self._make_dispatch_cb(entity_id),
            )
            self._debouncers[entity_id] = deb
        else:
            deb._delay_s = total_s  # noqa: SLF001
        deb.trigger()

    def _consume_echo(self, entity_id: str, new_state: "State") -> bool:
        kind = entity_kind(entity_id)
        payload = extract_payload(new_state, kind)
        bucket = self._echo_map.get(entity_id)
        if not bucket:
            return False
        now = time.monotonic()
        for key, ts in list(bucket.items()):
            if (now - ts) > DEFAULT_ECHO_TTL_S:
                bucket.pop(key, None)
        # Exact signature match
        sig = payload_signature(payload)
        if sig in bucket:
            bucket.pop(sig, None)
            _LOGGER.debug("回环抑制: %s", entity_id)
            return True
        # Approximate match against stamped expected light payloads
        expected_list = self._echo_expected.get(entity_id) or []
        keep: list[tuple[dict[str, Any], float]] = []
        matched = False
        for expected, ts in expected_list:
            if (now - ts) > DEFAULT_ECHO_TTL_S:
                continue
            if light_payloads_close(payload, expected) or payloads_equal(payload, expected):
                matched = True
                continue
            keep.append((expected, ts))
        self._echo_expected[entity_id] = keep
        if matched:
            _LOGGER.debug("回环抑制(近似): %s", entity_id)
            return True
        # Soft state-only match is OK for plain toggles, NEVER for lights.
        if kind != "light":
            soft = payload_signature({"state": payload.get("state")})
            if soft in bucket:
                bucket.pop(soft, None)
                _LOGGER.debug("回环抑制(软): %s", entity_id)
                return True
        return False

    def _make_dispatch_cb(self, source_entity_id: str) -> Callable[[], None]:
        def _cb() -> None:
            # Always schedule from the event loop (debouncer guarantees that).
            self.hass.async_create_task(
                self._async_dispatch(source_entity_id, self._seq),
                name=f"symi_sync_dispatch_{source_entity_id}",
            )

        return _cb

    async def _async_dispatch(self, source_entity_id: str, observed_seq: int) -> None:
        if self._stopping or not self._group.get("enabled", True):
            return
        state = self.hass.states.get(source_entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            return

        entities = self._entity_ids()
        if source_entity_id not in entities:
            return

        direction = self._group.get(CONF_DIRECTION, DIRECTION_TWO_WAY)
        if direction == DIRECTION_ONE_WAY and entities.index(source_entity_id) != 0:
            return

        source_kind = entity_kind(source_entity_id)
        if source_kind not in SYNCABLE_DOMAINS:
            return
        payload = extract_payload(state, source_kind)

        # Refresh master authority + group latch for the whole apply window (cloud lag).
        now = time.monotonic()
        self._authority_until[source_entity_id] = now + AUTHORITY_HOLD_S
        self._last_master_payload[source_entity_id] = payload
        self._group_master = source_entity_id
        self._group_desired = dict(payload)
        self._group_desired_until = now + GROUP_LATCH_S

        for target in entities:
            if target == source_entity_id:
                continue
            target_kind = entity_kind(target)
            converted = convert_payload(
                payload,
                source_kind,
                target_kind,
                self.hass.states.get(target),
            )
            if converted is None:
                _LOGGER.debug("跳过不兼容: %s(%s) -> %s(%s)", source_entity_id, source_kind, target, target_kind)
                continue
            self._enqueue(target, converted, source_kind, target_kind, observed_seq)

    def _enqueue(
        self,
        target_entity_id: str,
        payload: dict[str, Any],
        source_kind: str,
        target_kind: str,
        seq: int,
    ) -> None:
        now = time.monotonic()
        # 死循环熔断：被熔断的实体在冷却期内跳过一切写入，冷却后自动恢复。
        until = self._loop_disabled.get(target_entity_id)
        if until is not None:
            if now < until:
                return
            self._loop_disabled.pop(target_entity_id, None)
            _LOGGER.info("熔断冷却结束，自动恢复同步: %s", target_entity_id)
        # Never overwrite a recent master click with follower feedback.
        if now < self._authority_until.get(target_entity_id, 0.0):
            _LOGGER.debug("保护主控，跳过写回: %s", target_entity_id)
            return

        # Do not queue a write that would reverse the latched group desire.
        if (
            now < self._group_desired_until
            and self._group_desired is not None
            and self._states_conflict(payload, self._group_desired)
        ):
            _LOGGER.debug(
                "跳过与主控意图冲突的写入: %s payload=%s desired=%s",
                target_entity_id,
                payload.get("state"),
                self._group_desired.get("state"),
            )
            return

        tgt_state = self.hass.states.get(target_entity_id)
        if tgt_state is None or tgt_state.state in ("unavailable", "unknown"):
            # 目标不可用时入队只会让 worker 空转重试 —— 直接跳过，等它恢复后
            # 由启动对齐或下一次状态变化接管。
            _LOGGER.debug("目标当前不可用，跳过入队: %s", target_entity_id)
            return
        tgt_payload = extract_payload(tgt_state, target_kind)
        if payloads_equal(tgt_payload, payload) or (
            tgt_payload.get("state") == payload.get("state")
            and source_kind != target_kind
            and set(payload.keys()) <= {"state"}
        ):
            return

        # Do not drop here — worker paces by local/cloud outbound interval.
        try:
            self._queue.put_nowait(
                (target_entity_id, payload, source_kind, target_kind, seq)
            )
        except asyncio.QueueFull:
            _LOGGER.error("同步队列已满,丢弃 -> %s", target_entity_id)

    async def _worker_loop(self) -> None:
        while not self._stopping:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return
            target, payload, source_kind, target_kind, seq = item
            try:
                await self._apply_with_retry(target, payload, target_kind, seq)
                self._last_outbound_ts[target] = time.monotonic()
                self.hass.bus.async_fire(
                    EVENT_SYNC_APPLIED,
                    {
                        "group": self._group.get(CONF_NAME),
                        "target": target,
                        "source_kind": source_kind,
                        "target_kind": target_kind,
                        "seq": seq,
                    },
                )
            except ApplyFailedError as err:
                if "unavailable" in str(err).lower():
                    last = self._unavailable_log_ts.get(target, 0.0)
                    if time.monotonic() - last >= UNAVAILABLE_LOG_COOLDOWN_S:
                        self._unavailable_log_ts[target] = time.monotonic()
                        _LOGGER.warning(
                            "同步暂不可用(已放弃本轮): target=%s", target
                        )
                else:
                    _LOGGER.error("同步永久失败: target=%s err=%s", target, err)
            except Exception:  # pragma: no cover
                _LOGGER.exception("同步 worker 异常")

    async def _apply_with_retry(
        self,
        target_entity_id: str,
        payload: dict[str, Any],
        kind: str,
        seq: int,
    ) -> None:
        async with self._inflight_sem:
            lock = self._entity_locks.setdefault(target_entity_id, asyncio.Lock())
            async with lock:
                if self._stopping:
                    return
                last_err: Exception | None = None
                for attempt in range(1, DEFAULT_RETRY_ATTEMPTS + 1):
                    try:
                        await self._apply_once(target_entity_id, payload, kind)
                        return
                    except EntityUnavailableError as err:
                        # 目标不可用是确定性失败：重试 5 次只会占满 worker
                        # 并刷 5 行日志，直接交给上层记一条冷却告警。
                        raise ApplyFailedError(
                            f"{target_entity_id}: unavailable ({err})"
                        ) from err
                    except Exception as err:  # noqa: BLE001
                        last_err = err
                        _LOGGER.warning(
                            "同步失败(%d/%d): %s %s",
                            attempt,
                            DEFAULT_RETRY_ATTEMPTS,
                            target_entity_id,
                            err,
                        )
                    await asyncio.sleep(DEFAULT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
                raise ApplyFailedError(f"{target_entity_id}: {last_err}")

    async def _apply_once(
        self, target_entity_id: str, payload: dict[str, Any], kind: str
    ) -> None:
        now = time.monotonic()
        if now < self._authority_until.get(target_entity_id, 0.0):
            _LOGGER.debug("保护主控，取消写入: %s", target_entity_id)
            return
        # Stale queue item after a newer opposite master click.
        if (
            now < self._group_desired_until
            and self._group_desired is not None
            and self._states_conflict(payload, self._group_desired)
        ):
            _LOGGER.debug(
                "取消过期写入: %s payload=%s desired=%s",
                target_entity_id,
                payload.get("state"),
                self._group_desired.get("state"),
            )
            return

        state = self.hass.states.get(target_entity_id)
        if state is None:
            raise EntityUnavailableError(target_entity_id)
        if state.state in ("unavailable", "unknown"):
            raise EntityUnavailableError(target_entity_id)

        calls = resolve_service_calls(kind, payload)
        if not calls:
            return

        expected = dict(payload)
        bucket = self._echo_map.setdefault(target_entity_id, {})
        bucket[payload_signature(expected)] = now
        if kind != "light":
            bucket[payload_signature({"state": expected.get("state")})] = now
        self._echo_expected.setdefault(target_entity_id, []).append((expected, now))

        # Follower quiet: ignore its state_changed as sync sources (no promote).
        self._last_written[target_entity_id] = dict(expected)
        self._quiet_until[target_entity_id] = now + FOLLOWER_QUIET_S
        deb = self._debouncers.get(target_entity_id)
        if deb is not None:
            deb.cancel()
        old_h = self._quiet_handles.pop(target_entity_id, None)
        if old_h is not None:
            old_h.cancel()

        def _schedule_after_quiet(eid: str = target_entity_id) -> None:
            self.hass.async_create_task(
                self._after_follower_quiet(eid),
                name=f"symi_after_quiet_{eid}",
            )

        self._quiet_handles[target_entity_id] = self.hass.loop.call_later(
            FOLLOWER_QUIET_S + 0.05, _schedule_after_quiet
        )

        domain, service, data = calls[0]
        data = {**data, "entity_id": target_entity_id}

        # Global pacing: cloud serialized; local unified serial gate.
        # 批量场景（几十台同时动作）会产生大量并发本地写入，per-target 独立计时
        # 会让全部帧同时涌入串口/网关（超过 MCU 串口缓冲导致丢帧）——
        # 本地写入统一走一个串行 gate（outbound_ms 间隔顺序下发）。
        path = entity_path_kind(self.hass, target_entity_id)
        interval = outbound_interval_s(self.hass, path)
        gate_key = "cloud" if path != "local" else "local_serial"
        _LOGGER.debug(
            "出队写入 path=%s interval=%.3fs -> %s", path, interval, target_entity_id
        )
        # 一条状态变化可能需求多个服务调用（空调 = 设定温度 + 风速 + 摆风）。
        # HA 的 climate.set_temperature 不接受 fan_mode，必须拆成独立调用，
        # 否则「温度变了、风速丢了」，对端下一轮又回写风速 —— 慢性乒乓。
        for domain, service, data in calls:
            data = {**data, "entity_id": target_entity_id}
            if path != "local":
                await self._paced_call(gate_key, interval, domain, service, data)
            else:
                await self._pace_outbound(gate_key, interval)
                await self.hass.services.async_call(
                    domain, service, data, blocking=True
                )
        self._record_write(target_entity_id)

    def _record_write(self, entity_id: str) -> None:
        """记录一次成功写入；滑动窗口内次数超限则熔断该实体。"""
        now = time.monotonic()
        dq = self._write_times.setdefault(entity_id, deque())
        dq.append(now)
        while dq and now - dq[0] > LOOP_WINDOW_S:
            dq.popleft()
        if len(dq) >= LOOP_BREAKER_LIMIT:
            dq.clear()
            self._loop_disabled[entity_id] = now + LOOP_COOLDOWN_S
            _LOGGER.error(
                "检测到疑似同步死循环：%s 在 %.0f 秒内被写入 %d 次，已熔断该实体 %.0f 秒"
                "（其余实体不受影响，冷却结束后自动恢复）",
                entity_id,
                LOOP_WINDOW_S,
                LOOP_BREAKER_LIMIT,
                LOOP_COOLDOWN_S,
            )
            bus = getattr(self.hass, "bus", None)
            if bus is not None:
                bus.async_fire(
                    EVENT_LOOP_BREAKER,
                    {"group": self._group.get(CONF_NAME), "entity_id": entity_id,
                     "window_s": LOOP_WINDOW_S, "limit": LOOP_BREAKER_LIMIT,
                     "cooldown_s": LOOP_COOLDOWN_S},
                )

    async def _paced_call(
        self,
        gate_key: str,
        interval_s: float,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> None:
        """Pace cloud starts without holding the lock during the slow RPC."""
        bucket = self.hass.data.setdefault(DOMAIN, {})
        gate = bucket.setdefault(
            "_outbound_gate",
            {"lock": asyncio.Lock(), "last": {}, "sem": None},
        )
        if gate.get("sem") is None:
            gate["sem"] = asyncio.Semaphore(CLOUD_WRITE_CONCURRENCY)
        async with gate["lock"]:
            last = float(gate["last"].get(gate_key, 0.0))
            now = time.monotonic()
            wait = interval_s - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            # Stamp start time so the next waiter spaces from start, not end.
            gate["last"][gate_key] = time.monotonic()
        async with gate["sem"]:
            await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _pace_outbound(self, gate_key: str, interval_s: float) -> None:
        if interval_s <= 0:
            return
        bucket = self.hass.data.setdefault(DOMAIN, {})
        gate = bucket.setdefault(
            "_outbound_gate",
            {"lock": asyncio.Lock(), "last": {}},
        )
        async with gate["lock"]:
            last = float(gate["last"].get(gate_key, 0.0))
            now = time.monotonic()
            wait = interval_s - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            gate["last"][gate_key] = time.monotonic()

    def describe(self) -> dict[str, Any]:
        from .settings import get_settings

        return {
            "name": self._group.get(CONF_NAME),
            "entities": self._entity_ids(),
            "direction": self._group.get(CONF_DIRECTION, DIRECTION_TWO_WAY),
            "enabled": self._group.get("enabled", True),
            "queue_size": self._queue.qsize(),
            "echo_buckets": {k: len(v) for k, v in self._echo_map.items()},
            "seq": self._seq,
            "settings": get_settings(self.hass),
        }


def resolve_service_call(
    kind: str, payload: dict[str, Any]
) -> tuple[str, str, dict[str, Any]] | None:
    """Map payload -> (domain, service, data)."""
    state = str(payload.get("state", ""))
    data: dict[str, Any] = {}

    if kind == "light":
        if state == "off" or _is_on(state) is False:
            return "light", "turn_off", data
        # Prefer the color descriptor already selected by extract_payload.
        # Never send more than one color descriptor (HA rejects it).
        if "brightness" in payload and payload["brightness"] is not None:
            data["brightness"] = int(payload["brightness"])
        if "effect" in payload and payload["effect"] is not None:
            data["effect"] = payload["effect"]
        if payload.get("rgb_color") is not None:
            data["rgb_color"] = list(payload["rgb_color"])
        elif payload.get("hs_color") is not None:
            data["hs_color"] = list(payload["hs_color"])
        elif payload.get("xy_color") is not None:
            data["xy_color"] = list(payload["xy_color"])
        elif payload.get("color_temp_kelvin") is not None:
            data["color_temp_kelvin"] = int(payload["color_temp_kelvin"])
        elif payload.get("color_temp") is not None:
            data["color_temp"] = payload["color_temp"]
        return "light", "turn_on", data

    if kind in ("switch", "input_boolean", "automation", "siren"):
        if _is_on(state) is False or state == "off":
            return kind, "turn_off", data
        return kind, "turn_on", data

    if kind == "script":
        if _is_on(state) is False or state == "off":
            return "script", "turn_off", data
        return "script", "turn_on", data

    if kind == "cover":
        if "current_cover_position" in payload:
            return "cover", "set_cover_position", {
                "position": int(payload["current_cover_position"])
            }
        if "current_cover_tilt_position" in payload:
            return "cover", "set_cover_tilt_position", {
                "tilt_position": int(payload["current_cover_tilt_position"])
            }
        if state in ("open", "opening"):
            return "cover", "open_cover", data
        if state in ("closed", "closing"):
            return "cover", "close_cover", data
        if state == "stop":
            return "cover", "stop_cover", data
        return None

    if kind == "valve":
        if "current_position" in payload:
            return "valve", "set_valve_position", {"position": int(payload["current_position"])}
        if state in ("open", "opening", "on"):
            return "valve", "open_valve", data
        if state in ("closed", "closing", "off"):
            return "valve", "close_valve", data
        return None

    if kind == "fan":
        if _is_on(state) is False or state == "off":
            return "fan", "turn_off", data
        # Prefer dedicated setters when only speed/preset changed (新风常见).
        if "percentage" in payload and len([k for k in payload if k != "state"]) == 1:
            return "fan", "set_percentage", {"percentage": int(payload["percentage"])}
        if "preset_mode" in payload and "percentage" not in payload:
            return "fan", "set_preset_mode", {"preset_mode": payload["preset_mode"]}
        for key in ("percentage", "preset_mode", "oscillating", "direction"):
            if key in payload:
                data[key] = payload[key]
        return "fan", "turn_on", data

    if kind == "climate":
        # Prefer set_temperature (may include hvac_mode) so setpoint + mode
        # land in one call — important for KNX floor heating / AC.
        if state == "off" or payload.get("hvac_mode") == "off":
            return "climate", "set_hvac_mode", {"hvac_mode": "off"}
        if any(k in payload for k in ("temperature", "target_temp_high", "target_temp_low")):
            for k in ("temperature", "target_temp_high", "target_temp_low", "hvac_mode"):
                if k in payload and payload[k] is not None:
                    data[k] = payload[k]
            return "climate", "set_temperature", data
        if "hvac_mode" in payload and payload["hvac_mode"] is not None:
            return "climate", "set_hvac_mode", {"hvac_mode": payload["hvac_mode"]}
        if "fan_mode" in payload:
            return "climate", "set_fan_mode", {"fan_mode": payload["fan_mode"]}
        if "swing_mode" in payload:
            return "climate", "set_swing_mode", {"swing_mode": payload["swing_mode"]}
        if "humidity" in payload:
            return "climate", "set_humidity", {"humidity": payload["humidity"]}
        return "climate", "turn_on", data

    if kind == "lock":
        if state == "locked" or state == "off":
            return "lock", "lock", data
        return "lock", "unlock", data

    if kind == "media_player":
        if "volume_level" in payload:
            return "media_player", "volume_set", {"volume_level": payload["volume_level"]}
        if "is_volume_muted" in payload:
            return "media_player", "volume_mute", {"is_volume_muted": payload["is_volume_muted"]}
        if state in ("playing", "on"):
            return "media_player", "media_play", data
        if state in ("paused",):
            return "media_player", "media_pause", data
        if state in ("idle", "off", "standby"):
            return "media_player", "turn_off", data
        return None

    if kind in ("number", "input_number"):
        value = payload.get("value", payload.get("state"))
        if value is None:
            return None
        return kind, "set_value", {"value": float(value)}

    if kind in ("select", "input_select"):
        option = payload.get("option", payload.get("state"))
        if option is None:
            return None
        return kind, "select_option", {"option": option}

    if kind == "input_text":
        value = payload.get("value", payload.get("state"))
        if value is None:
            return None
        return "input_text", "set_value", {"value": str(value)}

    if kind == "text":
        value = payload.get("value", payload.get("state"))
        if value is None:
            return None
        return "text", "set_value", {"value": str(value)}

    if kind == "button":
        return "button", "press", data

    if kind == "humidifier":
        if state == "off":
            return "humidifier", "turn_off", data
        if "humidity" in payload:
            return "humidifier", "set_humidity", {"humidity": payload["humidity"]}
        if "mode" in payload:
            return "humidifier", "set_mode", {"mode": payload["mode"]}
        return "humidifier", "turn_on", data

    if kind == "water_heater":
        if state == "off":
            return "water_heater", "turn_off", data
        if "temperature" in payload:
            return "water_heater", "set_temperature", {"temperature": payload["temperature"]}
        if "operation_mode" in payload:
            return "water_heater", "set_operation_mode", {
                "operation_mode": payload["operation_mode"]
            }
        return "water_heater", "turn_on", data

    if kind == "vacuum":
        if state in ("docked", "idle", "off"):
            return "vacuum", "return_to_base", data
        return "vacuum", "start", data

    if kind == "scene":
        return "scene", "turn_on", data

    return None


def _climate_service_calls(
    payload: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    """One climate state change may need several service calls.

    ``climate.set_temperature`` carries the setpoint (and optionally the HVAC
    mode) but never the fan speed or swing — those are separate services.
    Sending only the first call silently drops the rest of the payload, and the
    peer's unchanged value comes back as a "difference" on the next pass.
    """
    state = str(payload.get("state", ""))
    if state == "off" or payload.get("hvac_mode") == "off":
        return [("climate", "set_hvac_mode", {"hvac_mode": "off"})]

    calls: list[tuple[str, str, dict[str, Any]]] = []
    has_setpoint = any(
        key in payload
        for key in ("temperature", "target_temp_high", "target_temp_low")
    )
    if has_setpoint:
        data: dict[str, Any] = {}
        for key in ("temperature", "target_temp_high", "target_temp_low", "hvac_mode"):
            if payload.get(key) is not None:
                data[key] = payload[key]
        calls.append(("climate", "set_temperature", data))
    elif payload.get("hvac_mode") is not None:
        calls.append(("climate", "set_hvac_mode", {"hvac_mode": payload["hvac_mode"]}))

    if payload.get("fan_mode") is not None:
        calls.append(("climate", "set_fan_mode", {"fan_mode": payload["fan_mode"]}))
    if payload.get("swing_mode") is not None:
        calls.append(("climate", "set_swing_mode", {"swing_mode": payload["swing_mode"]}))
    if payload.get("humidity") is not None:
        calls.append(("climate", "set_humidity", {"humidity": payload["humidity"]}))
    if not calls:
        calls.append(("climate", "turn_on", {}))
    return calls


def resolve_service_calls(
    kind: str, payload: dict[str, Any]
) -> list[tuple[str, str, dict[str, Any]]]:
    """Map a payload to the full list of service calls it requires."""
    if kind == "climate":
        return _climate_service_calls(payload)
    call = resolve_service_call(kind, payload)
    return [call] if call is not None else []
