"""Shared plumbing for agents: an execution trace every claim carries."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceEntry:
    agent: str
    kind: str              # "llm" | "deterministic" | "human"
    status: str            # "ok" | "warning" | "error"
    duration_ms: int
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


class Trace:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []

    def add(self, agent: str, kind: str, status: str, started: float, summary: str, **details: Any) -> None:
        self.entries.append(TraceEntry(agent, kind, status, int((time.perf_counter() - started) * 1000), summary, details))

    def add_ms(self, agent: str, kind: str, status: str, duration_ms: int, summary: str, **details: Any) -> None:
        self.entries.append(TraceEntry(agent, kind, status, duration_ms, summary, details))
