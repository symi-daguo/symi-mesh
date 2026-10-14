"""sensor platform for the Symi Mesh integration."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SymiMeshCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from the gateway coordinator."""
    coordinator: SymiMeshCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.register_platform("sensor", async_add_entities)
