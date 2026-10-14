"""Domain-wide sync timing settings (persisted via HA Store)."""
from __future__ import annotations

from typing import Any

from .const import (
    CONF_SYNC_DEBOUNCE_MS,
    CONF_SYNC_OUTBOUND_MS,
    DEFAULT_SYNC_DEBOUNCE_MS,
    DEFAULT_SYNC_OUTBOUND_MS,
    DOMAIN,
)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.sync_settings"

_KEYS = (CONF_SYNC_DEBOUNCE_MS, CONF_SYNC_OUTBOUND_MS)


def _defaults() -> dict[str, int]:
    return {
        CONF_SYNC_DEBOUNCE_MS: DEFAULT_SYNC_DEBOUNCE_MS,
        CONF_SYNC_OUTBOUND_MS: DEFAULT_SYNC_OUTBOUND_MS,
    }


async def async_load_settings(hass: Any) -> dict[str, int]:
    from homeassistant.helpers.storage import Store

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    raw = await store.async_load() or {}
    merged = _defaults()
    for key in _KEYS:
        if key in raw:
            try:
                merged[key] = max(0, int(raw[key]))
            except (TypeError, ValueError):
                pass
    hass.data.setdefault(DOMAIN, {})["_settings"] = merged
    return merged


def get_settings(hass: Any) -> dict[str, int]:
    cached = hass.data.get(DOMAIN, {}).get("_settings")
    if isinstance(cached, dict):
        return {**_defaults(), **cached}
    return _defaults()


async def async_save_settings(hass: Any, values: dict[str, int]) -> dict[str, int]:
    from homeassistant.helpers.storage import Store

    merged = _defaults()
    for key in _KEYS:
        if key in values:
            merged[key] = max(0, int(values[key]))
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(merged)
    hass.data.setdefault(DOMAIN, {})["_settings"] = merged
    return merged


def outbound_interval_s(hass: Any, path_kind: str) -> float:
    s = get_settings(hass)
    if path_kind == "local":
        return s[CONF_SYNC_OUTBOUND_MS] / 1000.0
    # Cloud peers get a slower, fixed cadence (safe default).
    return max(s[CONF_SYNC_OUTBOUND_MS] * 4, 200) / 1000.0


def debounce_s(hass: Any, path_kind: str) -> float:
    s = get_settings(hass)
    if path_kind == "local":
        return s[CONF_SYNC_DEBOUNCE_MS] / 1000.0
    return max(s[CONF_SYNC_DEBOUNCE_MS] * 3, 350) / 1000.0
