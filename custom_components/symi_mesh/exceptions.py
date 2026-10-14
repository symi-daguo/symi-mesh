"""Exceptions for SYMI 双向同步."""
from __future__ import annotations


class SymiSyncError(Exception):
    """Base error."""


class EntityUnavailableError(SymiSyncError):
    """Entity is missing or unavailable."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"{entity_id} unavailable")


class ApplyFailedError(SymiSyncError):
    """Apply failed after retries."""
