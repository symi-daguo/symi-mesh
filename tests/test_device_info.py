"""设备层级参数的单测：适配 via_device_id / via_device 两种 HA API。

背景：2025.11 的 `DeviceRegistry.async_get_or_create` 只接受 `via_device`，
2026.11 只接受 `via_device_id`。硬编码任一个都会让整台设备的实体全部建不出来
（实测 2025.11.3 下 67 台设备一个都没创建）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.symi_mesh import entity as ent


def _coordinator() -> SimpleNamespace:
    return SimpleNamespace(
        gateway_device_id="gateway-ulid",
        gateway_identifier=lambda entry: "gw-ident",
        entry=object(),
    )


BASE = {"identifiers": {("symi_mesh", "aabbcc001122")}, "name": "测试设备"}


def _get(info, key):
    """DeviceInfo 在部分 HA 版本是 TypedDict（dict），在新版本是 dataclass。"""
    if isinstance(info, dict):
        return info.get(key)
    return getattr(info, key, None)


def test_detection_matches_current_ha() -> None:
    """探测结果必须与当前 HA 的实际签名一致。"""
    import inspect

    from homeassistant.helpers.device_registry import DeviceRegistry

    actual = "via_device_id" in inspect.signature(
        DeviceRegistry.async_get_or_create
    ).parameters
    assert ent._VIA_DEVICE_ID_SUPPORTED is actual


def test_uses_via_device_id_when_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ent, "_VIA_DEVICE_ID_SUPPORTED", True)
    info = ent.build_device_info(_coordinator(), dict(BASE))
    assert _get(info, "via_device_id") == "gateway-ulid"
    assert _get(info, "via_device") is None


def test_falls_back_to_via_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ent, "_VIA_DEVICE_ID_SUPPORTED", False)
    info = ent.build_device_info(_coordinator(), dict(BASE))
    assert _get(info, "via_device") == ("symi_mesh", "gw-ident")
    assert _get(info, "via_device_id") is None


def test_without_gateway_device_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """网关设备还没注册好时也不能崩，只是暂时没有层级关系。"""
    monkeypatch.setattr(ent, "_VIA_DEVICE_ID_SUPPORTED", True)
    coord = _coordinator()
    coord.gateway_device_id = None
    info = ent.build_device_info(coord, dict(BASE))
    assert _get(info, "via_device_id") is None
