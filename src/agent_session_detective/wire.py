"""Parse Kimi Code wire.jsonl session logs into typed events.

Two on-disk formats exist:

- CLI format (protocol 1.9): each record carries a ``message`` object with a
  ``type`` (TurnBegin, ToolCall, StatusUpdate, CompactionBegin, ...) and a
  ``payload``. Main log at the session root; subagent logs under
  ``subagents/<id>/wire.jsonl``.
- Desktop format (protocol 1.5): typed records such as ``turn.prompt``,
  ``context.append_loop_event`` (step.begin, content.part, tool.call,
  tool.result), ``token_counting.measured``, ``subagent.spawned``. Main log
  at ``agents/main/wire.jsonl``; subagent logs under ``agents/agent-*/``.

Desktop records are translated into the same Event model the CLI format
produces, so downstream modules only see one shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
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
        if self.type == "UsageRecord":
            p = self.payload
            return "usage in=%d out=%d cache=%d" % (
                (p.get("input_other") or 0) + (p.get("input_cache_read") or 0),
                p.get("output") or 0,
                p.get("input_cache_read") or 0,
            )
        if self.type == "LLMRequest":
            h = str(self.payload.get("system_prompt_hash") or "")
            return "llm.request %s msgs=%s syshash=%s" % (
                self.payload.get("model"),
                self.payload.get("message_count"),
                h[:8],
            )
        if self.type == "TurnTokens":
            return "turn %s ctx=%s tokens" % (
                self.payload.get("turn_id"),
                self.payload.get("tokens"),
            )
        return self.type


@dataclass
class Session:
    directory: Path
    events: List[Event] = field(default_factory=list)
    protocol_version: Optional[str] = None
    compaction_telemetry_available: bool = True

    def turns(self) -> List[Event]:
        return [e for e in self.events if e.type == "TurnBegin"]


def _translate_desktop(record: dict) -> Optional[tuple]:
    """Map one desktop-format record to (type, payload), or None to skip."""
    rtype = record.get("type")
    if rtype == "turn.prompt":
        origin = record.get("origin") or {}
        if origin.get("kind", "user") != "user":
            return None
        return "TurnBegin", {"user_input": record.get("input") or []}
    if rtype == "context.append_loop_event":
        event = record.get("event") or {}
        etype = event.get("type")
        if etype == "step.begin":
            return "StepBegin", {"n": event.get("step")}
        if etype == "content.part":
            return "ContentPart", event.get("part") or {}
        if etype == "tool.call":
            return "ToolCall", {
                "id": event.get("toolCallId"),
                "function": {
                    "name": event.get("name"),
                    "arguments": json.dumps(event.get("args") or {}, ensure_ascii=False),
                },
            }
        if etype == "tool.result":
            return "ToolResult", {
                "tool_call_id": event.get("toolCallId"),
                "return_value": event.get("result") or {},
            }
        return None
    if rtype == "token_counting.measured":
        return "StatusUpdate", {
            "context_tokens": record.get("tokens"),
            "context_usage": None,
        }
    if rtype == "token_counting.turn_recorded":
        if record.get("agentId", "main") != "main":
            return None
        return "TurnTokens", {
            "turn_id": record.get("turnId", 0),
            "tokens": record.get("tokens", 0),
        }
    if rtype == "usage.record":
        usage = record.get("usage") or {}
        return "UsageRecord", {
            "input_other": usage.get("inputOther", 0),
            "output": usage.get("output", 0),
            "input_cache_read": usage.get("inputCacheRead", 0),
            "input_cache_creation": usage.get("inputCacheCreation", 0),
            "model": record.get("model", ""),
            "agent_id": record.get("agentId", ""),
            "usage_scope": record.get("usageScope", ""),
        }
    if rtype == "llm.request":
        return "LLMRequest", {
            "kind": record.get("kind", ""),
            "model": record.get("model", ""),
            "agent_id": record.get("agentId", ""),
            "system_prompt_hash": record.get("systemPromptHash", ""),
            "tools_hash": record.get("toolsHash", ""),
            "message_count": record.get("messageCount", 0),
            "turn_step": record.get("turnStep", ""),
        }
    if isinstance(rtype, str) and "compact" in rtype.lower():
        return "CompactionBegin" if "begin" in rtype.lower() or "start" in rtype.lower() else "CompactionEnd", {}
    return None


def parse_wire(path: Path, origin: str) -> Iterator[Event]:
    seq = 0
    desktop: Optional[bool] = None
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
            rtype = record.get("type")
            if rtype == "metadata":
                continue
            if desktop is None:
                desktop = "message" not in record
            if desktop:
                ts = record.get("time")
                translated = _translate_desktop(record)
                if translated is None:
                    continue
                etype, payload = translated
                yield Event(
                    ts=ts / 1000.0 if isinstance(ts, (int, float)) else None,
                    type=etype,
                    payload=payload,
                    origin=origin,
                    source=path,
                    seq=seq,
                )
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


def _qoder_timestamp(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_qoder_transcript(path: Path, origin: str = "main") -> Iterator[Event]:
    """Normalize a Qoder project transcript into existing event types."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for seq, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue
            ts = _qoder_timestamp(record.get("timestamp"))
            if record["type"] == "user":
                user_input = [part for part in content if isinstance(part, dict) and part.get("type") == "text"]
                if user_input:
                    yield Event(ts, "TurnBegin", {"user_input": user_input}, origin, path, seq)
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        yield Event(ts, "ToolResult", {
                            "tool_call_id": part.get("tool_use_id"),
                            "return_value": {
                                "output": part.get("content", ""),
                                "is_error": bool(part.get("is_error", False)),
                            },
                        }, origin, path, seq)
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    yield Event(ts, "ContentPart", {"type": "text", "text": part.get("text", "")}, origin, path, seq)
                elif part.get("type") == "thinking":
                    yield Event(ts, "ContentPart", {"type": "think", "think": part.get("thinking", "")}, origin, path, seq)
                elif part.get("type") == "tool_use":
                    yield Event(ts, "ToolCall", {
                        "id": part.get("id"),
                        "function": {
                            "name": part.get("name"),
                            "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False),
                        },
                    }, origin, path, seq)
            usage = message.get("usage")
            if isinstance(usage, dict):
                yield Event(ts, "UsageRecord", {
                    "input_other": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "input_cache_read": usage.get("cache_read_input_tokens", 0),
                    "input_cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "model": message.get("model", record.get("model", "")),
                }, origin, path, seq)


def load_session(directory: Path) -> Session:
    directory = Path(directory)
    if directory.is_file():
        session = Session(directory=directory.parent, compaction_telemetry_available=False)
        session.events.extend(parse_qoder_transcript(directory))
        session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
        return session
    session = Session(directory=directory)
    main_candidates = [directory / "wire.jsonl", directory / "agents" / "main" / "wire.jsonl"]
    for main_wire in main_candidates:
        if main_wire.exists():
            for record in parse_wire(main_wire, origin="main"):
                session.events.append(record)
            break
    sub_wires = list((directory / "subagents").glob("*/wire.jsonl")) if (directory / "subagents").is_dir() else []
    agents_dir = directory / "agents"
    if agents_dir.is_dir():
        sub_wires.extend(w for w in agents_dir.glob("agent-*/wire.jsonl"))
    for wire in sorted(sub_wires):
        origin = "subagent:%s" % wire.parent.name
        for record in parse_wire(wire, origin=origin):
            session.events.append(record)
    session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
    return session


def find_latest_qoder_transcript(base: Path) -> Optional[Path]:
    """Most recently modified Qoder transcript under the projects root."""
    transcripts = list(base.glob("*/*.jsonl"))
    return max(transcripts, key=lambda p: p.stat().st_mtime) if transcripts else None


def find_latest_session(base: Path) -> Optional[Path]:
    """Most recently modified session directory under the sessions root."""
    wires = list(base.glob("*/*/wire.jsonl")) + list(base.glob("*/*/agents/main/wire.jsonl"))
    if not wires:
        return None
    newest = max(wires, key=lambda p: p.stat().st_mtime)
    # CLI layout: <root>/<ws>/<session>/wire.jsonl — session is the wire's parent.
    # Agents layout: <root>/<ws>/<session>/agents/main/wire.jsonl — three levels up.
    return newest.parents[2] if newest.parent.name in ("main", "agent") else newest.parent
