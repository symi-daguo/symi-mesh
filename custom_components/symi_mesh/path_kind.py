"""Path-kind classification for sync targets.

The upstream symi_two_way_sync engine paces writes by path kind (local vs
cloud). For this integration every peer is a local-push entity (HA KNX
entities and symi_mesh entities both push), so the default is ``local``.
Known cloud platforms (xiaomi_home, tuya, ...) are still classified as
``cloud`` so the slower pacing applies when a user pairs against them.
"""
from __future__ import annotations

from typing import Any

# Config-entry domains whose writes go through a cloud RPC.
_CLOUD_DOMAINS: frozenset[str] = frozenset(
    {
        "xiaomi_home",
        "tuya",
        "smartthings",
        "hue",  # cloud API variant only; bridge is local push
        "broadlink",
        "yeelight",
        "smartir",
    }
)


def entity_path_kind(hass: Any, entity_id: str) -> str:
    """Return ``"cloud"`` for cloud-RPC platforms, else ``"local"``."""
    try:
        entry = hass.data.get("entity_components", {}).get(entity_id.split(".", 1)[0])
        # Fall back to the entity registry lookup below when component data
        # is unavailable.
        del entry
    except Exception:  # noqa: BLE001 - defensive
        pass

    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        entry_obj = registry.async_get(entity_id)
        if entry_obj is not None and entry_obj.platform in _CLOUD_DOMAINS:
            return "cloud"
    except Exception:  # noqa: BLE001 - defensive
        pass
    return "local"


def entities_path_summary(hass: Any, entities: list[str]) -> tuple[str, str]:
    """Return (label, tip) describing the path mix of a sync group."""
    if not entities:
        return "", "请选择至少两个实体。"

    kinds = {entity_path_kind(hass, e) for e in entities}
    if kinds == {"local"}:
        return "本地", "组内全部为本地推送实体，按本地限速（防抖快、出队快）。"
    if kinds == {"cloud"}:
        return "云端", "组内全部为云端实体，按云端限速（防抖慢、出队串行）。"
    return "混合", "组内本地与云端实体混合，各自按类型限速。"
