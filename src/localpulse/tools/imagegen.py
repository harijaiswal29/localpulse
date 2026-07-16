"""Image generation tool. P0 returns placeholder refs; swap in a real generator +
object storage without touching agent code."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol


class ImageGenTool(Protocol):
    def generate(self, prompt: str) -> str: ...


@dataclass
class MockImageGenTool:
    client_id: str
    prompts: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"images/{self.client_id}/{uuid.uuid4().hex[:12]}.png"
