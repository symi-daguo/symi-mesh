"""Typed runtime data for the Symi Mesh integration.

One domain hosts two kinds of config entries (a gateway link and a KNX sync
group), so runtime data is a single dataclass with a discriminator instead of
two entry types. Platform modules import the alias below and read
``entry.runtime_data.coordinator`` directly — no shared ``hass.data`` dict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import SymiMeshCoordinator
    from .knx_sync import SymiSyncEngine


@dataclass(slots=True)
class SymiMeshRuntime:
    """Objects that live as long as one config entry is loaded."""

    kind: str
    coordinator: SymiMeshCoordinator | None = None
    engine: SymiSyncEngine | None = None


SymiMeshConfigEntry: TypeAlias = ConfigEntry[SymiMeshRuntime]
