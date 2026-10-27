"""配置流单测：菜单、TCP 连接成功 / 失败、重复条目（Bronze config-flow-test-coverage）。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.symi_mesh import config_flow
from custom_components.symi_mesh.const import CONF_CONNECTION_TYPE, DOMAIN, KIND_GATEWAY

TCP_INPUT = {"host": "10.0.0.9", "port": 4196}


async def _open_tcp_form(hass: HomeAssistant) -> dict:
    """user 菜单 -> gateway 选择 TCP -> 进入 TCP 表单。"""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "gateway"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_TYPE: "tcp"}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "gateway_tcp"
    return result


async def test_user_menu_has_both_entries(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is data_entry_flow.FlowResultType.MENU
    assert set(result["menu_options"]) == {"gateway", "knx_sync"}


async def test_tcp_form_shown(hass: HomeAssistant) -> None:
    result = await _open_tcp_form(hass)
    assert result["errors"] == {}


async def test_tcp_entry_created(hass: HomeAssistant) -> None:
    result = await _open_tcp_form(hass)
    with patch.object(config_flow, "_probe_tcp", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], TCP_INPUT
        )

    assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Symi Mesh 网关 (10.0.0.9)"
    assert result["data"]["kind"] == KIND_GATEWAY
    assert result["data"]["host"] == "10.0.0.9"
    assert result["data"]["port"] == 4196


async def test_tcp_probe_failure_shows_error(hass: HomeAssistant) -> None:
    result = await _open_tcp_form(hass)
    with patch.object(config_flow, "_probe_tcp", AsyncMock(return_value=False)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], TCP_INPUT
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "gateway_tcp"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_tcp_host_required(hass: HomeAssistant) -> None:
    result = await _open_tcp_form(hass)
    with patch.object(config_flow, "_probe_tcp", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**TCP_INPUT, "host": "   "}
        )

    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {"host": "host_required"}


async def test_duplicate_tcp_entry_aborts(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        data={
            "kind": KIND_GATEWAY,
            CONF_CONNECTION_TYPE: "tcp",
            "host": "10.0.0.9",
            "port": 4196,
        },
        unique_id="tcp:10.0.0.9:4196",
    ).add_to_hass(hass)

    result = await _open_tcp_form(hass)
    with patch.object(config_flow, "_probe_tcp", AsyncMock(return_value=True)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], TCP_INPUT
        )

    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """让 HA 加载 custom_components 下的集成。"""
    yield
