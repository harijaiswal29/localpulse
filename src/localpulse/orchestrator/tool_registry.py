"""Tool Registry — the live catalogue of tools per client. Agents query it to
discover capabilities and never call integrations directly."""

from __future__ import annotations

from typing import Any


class ToolNotConnectedError(Exception):
    def __init__(self, client_id: str, tool_name: str):
        super().__init__(f"tool {tool_name!r} is not connected for client {client_id!r}")


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], Any] = {}

    def register(self, client_id: str, name: str, tool: Any) -> None:
        self._tools[(client_id, name)] = tool

    def get(self, client_id: str, name: str) -> Any:
        try:
            return self._tools[(client_id, name)]
        except KeyError:
            raise ToolNotConnectedError(client_id, name) from None

    def is_connected(self, client_id: str, name: str) -> bool:
        return (client_id, name) in self._tools

    def available(self, client_id: str) -> list[str]:
        return sorted(name for (cid, name) in self._tools if cid == client_id)
