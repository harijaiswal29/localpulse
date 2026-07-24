"""Model providers used as eval fixtures.

The red-team suite needs a model that misbehaves on purpose. Rather than mocking
the gateway's internals, these register as ordinary named providers — the agents
call them through exactly the path a real model takes, so what the suite proves
is about the engine's containment, not about a stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScriptedProvider:
    """Returns fixed text, ignoring the prompt.

    `replies` is consumed in order and the last entry repeats, which is what makes
    a retry observable: the Content Agent asks twice before giving up on a slot, so
    a provider that stays bad on the second attempt proves the slot was dropped
    rather than quietly re-rolled into something acceptable.
    """

    replies: list[str]
    calls: list[str] = field(default_factory=list)

    def complete(self, prompt: str, system: str, max_tokens: int) -> str:
        self.calls.append(prompt)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]
