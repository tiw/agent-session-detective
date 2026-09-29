"""Parse Kimi Code wire.jsonl session logs into typed events.

Wire format (protocol 1.9): one JSON object per line. The first line is a
metadata record; every subsequent line carries a ``message`` object with a
``type`` (TurnBegin, StepBegin, ContentPart, ToolCall, ToolResult,
StatusUpdate, CompactionBegin, CompactionEnd, SubagentEvent, ...) and a
``payload``. Subagent logs live in ``subagents/<id>/wire.jsonl`` inside the
session directory and are attributed to their parent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional

@dataclass
class Event:
    ts: Optional[float]
    type: str
    payload: dict
    origin: str  # "main" or "subagent:<id>"
    source: Path
    seq: int

    def text_preview(self, limit: int = 120) -> str:
        """Short human-readable summary of the payload."""
        if self.type == "ToolCall":
            fn = self.payload.get("function", {})
            return "call %s(%s)" % (fn.get("name", "?"), str(fn.get("arguments", ""))[:limit])
        if self.type == "ToolResult":
            out = self.payload.get("return_value", {}).get("output", "")
            return "result %s" % str(out)[:limit]
        if self.type == "ContentPart":
            p = self.payload
            kind = p.get("type")
            text = p.get(kind) if isinstance(kind, str) else None
            return "%s %s" % (kind or "content", str(text or "")[:limit])
        if self.type == "TurnBegin":
            parts = self.payload.get("user_input", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            return "user: %s" % text[:limit]
        if self.type == "StatusUpdate":
            return "ctx %s tokens" % self.payload.get("context_tokens")
        return self.type


@dataclass
class Session:
    directory: Path
    events: List[Event] = field(default_factory=list)
    protocol_version: Optional[str] = None

    def turns(self) -> List[Event]:
        return [e for e in self.events if e.type == "TurnBegin"]


def parse_wire(path: Path, origin: str) -> Iterator[Event]:
    seq = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq += 1
            if record.get("type") == "metadata":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            yield Event(
                ts=record.get("timestamp"),
                type=str(message.get("type", "Unknown")),
                payload=message.get("payload") or {},
                origin=origin,
                source=path,
                seq=seq,
            )


def load_session(directory: Path) -> Session:
    directory = Path(directory)
    session = Session(directory=directory)
    main_wire = directory / "wire.jsonl"
    if main_wire.exists():
        for record in parse_wire(main_wire, origin="main"):
            session.events.append(record)
    subagents_dir = directory / "subagents"
    if subagents_dir.is_dir():
        for wire in sorted(subagents_dir.glob("*/wire.jsonl")):
            origin = "subagent:%s" % wire.parent.name
            for record in parse_wire(wire, origin=origin):
                session.events.append(record)
    session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
    return session


def find_latest_session(base: Path) -> Optional[Path]:
    """Most recently modified session directory under the sessions root."""
    candidates = sorted(base.glob("*/*/wire.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1].parent if candidates else None
