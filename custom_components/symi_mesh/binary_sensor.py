"""binary_sensor platform for the Symi Mesh integration."""
from __future__ import annotations


from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .runtime import SymiMeshConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SymiMeshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary_sensor entities from the gateway coordinator."""
    coordinator = entry.runtime_data.coordinator
    if coordinator is None:  # pragma: no cover - entry without a gateway link
        return
    coordinator.register_platform("binary_sensor", async_add_entities)
