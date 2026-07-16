"""Web search / trends tool — festival context, local events. Stub for P0; the
Content Agent degrades gracefully without it (spec §12.1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class WebSearchTool(Protocol):
    def search(self, query: str) -> list[str]: ...


@dataclass
class MockWebSearchTool:
    client_id: str

    def search(self, query: str) -> list[str]:
        return []
