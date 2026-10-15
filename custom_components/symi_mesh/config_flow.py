"""Config flow for Symi Mesh: gateway setup + KNX sync groups."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BAUD_RATE,
    CONF_CONNECTION_TYPE,
    CONF_ENABLE_STATUS_REPORT,
    CONF_HOST,
    CONF_PORT,
    CONF_QUERY_ON_STARTUP,
    CONF_RECONNECT_INTERVAL,
    CONF_SERIAL_PORT,
    CONF_SYNC_DEBOUNCE_MS,
    CONF_SYNC_DIRECTION,
    CONF_SYNC_ENABLED,
    CONF_SYNC_ENTITIES,
    CONF_SYNC_NAME,
    CONF_SYNC_OUTBOUND_MS,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    DEFAULT_BAUD_RATE,
    DEFAULT_PORT,
    DEFAULT_RECONNECT_INTERVAL,
    DEFAULT_SYNC_DEBOUNCE_MS,
    DEFAULT_SYNC_OUTBOUND_MS,
    DOMAIN,
    KIND_GATEWAY,
    KIND_KNX_SYNC,
    SYNC_DIRECTION_ONE_WAY,
    SYNC_DIRECTION_TWO_WAY,
    SYNCABLE_DOMAINS,
)
from .path_kind import entities_path_summary
from .protocol import ProtocolHandler, parse_device_list_frame
from .settings import async_load_settings, async_save_settings, get_settings

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection probe (used to validate gateway parameters before creating entry)
# ---------------------------------------------------------------------------


async def _probe_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    handler = ProtocolHandler()
    frame = handler.build_read_device_list_frame()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except Exception:  # noqa: BLE001 - any failure means "cannot connect"
        return False
    try:
        writer.write(frame)
        await writer.drain()
        deadline = asyncio.get_event_loop().time() + 3.0
        while asyncio.get_event_loop().time() < deadline:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            if not chunk:
                return False
            for parsed in handler.add_data(chunk):
                if parse_device_list_frame(parsed) is not None:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _probe_serial(port_path: str, baud_rate: int, timeout: float = 5.0) -> bool:
    def _blocking_probe() -> bool:
        try:
            import serial

            handler = ProtocolHandler()
            with serial.Serial(port_path, baud_rate, timeout=2) as ser:
                ser.write(handler.build_read_device_list_frame())
                deadline = __import__("time").monotonic() + 3.0
                while __import__("time").monotonic() < deadline:
                    chunk = ser.read(256)
                    if not chunk:
                        continue
                    for parsed in handler.add_data(chunk):
                        if parse_device_list_frame(parsed) is not None:
                            return True
            return False
        except Exception:  # noqa: BLE001
            return False

    try:
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _blocking_probe),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001
        return False


def _detect_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports

        return [p.device for p in list_ports.comports()]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _connection_type_schema(default: str = CONNECTION_TCP) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CONNECTION_TYPE, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=[CONNECTION_TCP, CONNECTION_SERIAL],
                    mode=SelectSelectorMode.LIST,
                    translation_key="connection_type",
                )
            )
        }
    )


def _tcp_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT, DEFAULT_PORT))): int,
        }
    )


def _serial_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SERIAL_PORT, default=defaults.get(CONF_SERIAL_PORT, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(
                CONF_BAUD_RATE, default=int(defaults.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE))
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1200, max=921600, step=100, mode=NumberSelectorMode.BOX
                )
            ),
        }
    )


def _gateway_options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_RECONNECT_INTERVAL,
                default=int(defaults.get(CONF_RECONNECT_INTERVAL, DEFAULT_RECONNECT_INTERVAL)),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=60, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                )
            ),
            vol.Required(
                CONF_ENABLE_STATUS_REPORT,
                default=bool(defaults.get(CONF_ENABLE_STATUS_REPORT, True)),
            ): BooleanSelector(),
            vol.Required(
                CONF_QUERY_ON_STARTUP,
                default=bool(defaults.get(CONF_QUERY_ON_STARTUP, True)),
            ): BooleanSelector(),
        }
    )


def _sync_schema(hass: HomeAssistant, defaults: dict[str, Any]) -> vol.Schema:
    settings = get_settings(hass)
    return vol.Schema(
        {
            vol.Required(CONF_SYNC_NAME, default=defaults.get(CONF_SYNC_NAME, "")): TextSelector(),
            vol.Required(
                CONF_SYNC_ENTITIES, default=defaults.get(CONF_SYNC_ENTITIES, [])
            ): EntitySelector(
                EntitySelectorConfig(multiple=True, domain=list(SYNCABLE_DOMAINS))
            ),
            vol.Required(
                CONF_SYNC_DIRECTION,
                default=defaults.get(CONF_SYNC_DIRECTION, SYNC_DIRECTION_TWO_WAY),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[SYNC_DIRECTION_TWO_WAY, SYNC_DIRECTION_ONE_WAY],
                    mode=SelectSelectorMode.LIST,
                    translation_key="direction",
                )
            ),
            vol.Required(
                CONF_SYNC_ENABLED, default=bool(defaults.get(CONF_SYNC_ENABLED, True))
            ): BooleanSelector(),
            vol.Required(
                CONF_SYNC_DEBOUNCE_MS,
                default=int(defaults.get(CONF_SYNC_DEBOUNCE_MS, settings[CONF_SYNC_DEBOUNCE_MS])),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=5000, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="ms"
                )
            ),
            vol.Required(
                CONF_SYNC_OUTBOUND_MS,
                default=int(
                    defaults.get(CONF_SYNC_OUTBOUND_MS, settings[CONF_SYNC_OUTBOUND_MS])
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0, max=5000, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="ms"
                )
            ),
        }
    )


def _validate_sync(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    name = str(user_input.get(CONF_SYNC_NAME) or "").strip()
    entities = user_input.get(CONF_SYNC_ENTITIES) or []
    if not name:
        errors[CONF_SYNC_NAME] = "name_required"
    if not isinstance(entities, list) or len(entities) < 2:
        errors[CONF_SYNC_ENTITIES] = "need_two_entities"
    elif len(set(entities)) != len(entities):
        errors[CONF_SYNC_ENTITIES] = "duplicate_entities"
    return errors


def _normalize_sync_group(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_SYNC_NAME: str(user_input[CONF_SYNC_NAME]).strip(),
        CONF_SYNC_ENTITIES: list(user_input[CONF_SYNC_ENTITIES]),
        CONF_SYNC_DIRECTION: user_input.get(CONF_SYNC_DIRECTION, SYNC_DIRECTION_TWO_WAY),
        CONF_SYNC_ENABLED: bool(user_input.get(CONF_SYNC_ENABLED, True)),
    }


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class SymiMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the two entry flavours: gateway and knx_sync."""

    VERSION = 1

    def __init__(self) -> None:
        self._wizard: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ):
        return self.async_show_menu(
            step_id="user",
            menu_options=["gateway", "knx_sync"],
        )

    # -- gateway ------------------------------------------------------------

    async def async_step_gateway(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        if user_input is not None:
            connection_type = user_input[CONF_CONNECTION_TYPE]
            if connection_type == CONNECTION_SERIAL:
                return await self.async_step_gateway_serial()
            return await self.async_step_gateway_tcp()
        return self.async_show_form(
            step_id="gateway",
            data_schema=_connection_type_schema(),
            errors=errors,
        )

    async def async_step_gateway_tcp(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            if not host:
                errors[CONF_HOST] = "host_required"
            elif not await _probe_tcp(host, port):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"tcp:{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Symi Mesh 网关 ({host})",
                    data={
                        "kind": KIND_GATEWAY,
                        CONF_CONNECTION_TYPE: CONNECTION_TCP,
                        CONF_HOST: host,
                        CONF_PORT: port,
                    },
                )
        return self.async_show_form(
            step_id="gateway_tcp",
            data_schema=_tcp_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_gateway_serial(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        ports = _detect_serial_ports()
        if user_input is not None:
            port_path = str(user_input[CONF_SERIAL_PORT]).strip()
            baud_rate = int(user_input[CONF_BAUD_RATE])
            if not port_path:
                errors[CONF_SERIAL_PORT] = "serial_port_required"
            elif not await _probe_serial(port_path, baud_rate):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(f"serial:{port_path}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Symi Mesh 网关 ({port_path})",
                    data={
                        "kind": KIND_GATEWAY,
                        CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
                        CONF_SERIAL_PORT: port_path,
                        CONF_BAUD_RATE: baud_rate,
                    },
                )
        description_placeholders = {"ports": "、".join(ports) if ports else "未检测到"}
        return self.async_show_form(
            step_id="gateway_serial",
            data_schema=_serial_schema(user_input or {}),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    # -- knx sync ------------------------------------------------------------

    async def async_step_knx_sync(
        self, user_input: dict[str, Any] | None = None
    ):
        await async_load_settings(self.hass)
        errors: dict[str, str] = {}
        path_tip = "选择实体后将提示本地/云端类型；下方两项时间为全局（保存后全部同步组共用）。"
        defaults = dict(user_input or {})

        if user_input is not None:
            errors = _validate_sync(user_input)
            entities = list(user_input.get(CONF_SYNC_ENTITIES) or [])
            if entities:
                _, path_tip = entities_path_summary(self.hass, entities)
            if not errors:
                await async_save_settings(
                    self.hass,
                    {
                        CONF_SYNC_DEBOUNCE_MS: int(
                            user_input.get(CONF_SYNC_DEBOUNCE_MS, DEFAULT_SYNC_DEBOUNCE_MS)
                        ),
                        CONF_SYNC_OUTBOUND_MS: int(
                            user_input.get(CONF_SYNC_OUTBOUND_MS, DEFAULT_SYNC_OUTBOUND_MS)
                        ),
                    },
                )
                data = _normalize_sync_group(user_input)
                unique = "|".join(sorted(data[CONF_SYNC_ENTITIES]))
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = data[CONF_SYNC_NAME] or " <-> ".join(data[CONF_SYNC_ENTITIES][:2])
                return self.async_create_entry(title=title, data={"kind": KIND_KNX_SYNC, **data})
            defaults = dict(user_input)
        else:
            defaults = dict(get_settings(self.hass))

        return self.async_show_form(
            step_id="knx_sync",
            data_schema=_sync_schema(self.hass, defaults),
            errors=errors,
            description_placeholders={"path_tip": path_tip},
        )

    # -- options --------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        if config_entry.data.get("kind") == KIND_KNX_SYNC:
            return SymiSyncOptionsFlow()
        return SymiGatewayOptionsFlow()


class SymiGatewayOptionsFlow(OptionsFlow):
    """Gateway tuning (reconnect, reporting, startup query)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_gateway_options_schema(current),
        )


class SymiSyncOptionsFlow(OptionsFlow):
    """Edit a sync group; timings are global (shared by all groups)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ):
        await async_load_settings(self.hass)
        errors: dict[str, str] = {}
        entry = self.config_entry
        current = {**entry.data, **entry.options, **get_settings(self.hass)}
        entities = list(current.get(CONF_SYNC_ENTITIES) or [])
        _, path_tip = entities_path_summary(self.hass, entities)

        if user_input is not None:
            errors = _validate_sync(user_input)
            entities = list(user_input.get(CONF_SYNC_ENTITIES) or [])
            if entities:
                _, path_tip = entities_path_summary(self.hass, entities)
            if not errors:
                await async_save_settings(
                    self.hass,
                    {
                        CONF_SYNC_DEBOUNCE_MS: int(
                            user_input.get(CONF_SYNC_DEBOUNCE_MS, DEFAULT_SYNC_DEBOUNCE_MS)
                        ),
                        CONF_SYNC_OUTBOUND_MS: int(
                            user_input.get(CONF_SYNC_OUTBOUND_MS, DEFAULT_SYNC_OUTBOUND_MS)
                        ),
                    },
                )
                data = _normalize_sync_group(user_input)
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=data[CONF_SYNC_NAME],
                    data={"kind": KIND_KNX_SYNC, **data},
                    options={},
                )
                return self.async_create_entry(title="", data={})
            current = dict(user_input)

        return self.async_show_form(
            step_id="sync_init",
            data_schema=_sync_schema(self.hass, current),
            errors=errors,
            description_placeholders={"path_tip": path_tip},
        )
