"""Parse agent session logs into typed events.

Three on-disk formats are supported:

- Kimi CLI format (protocol 1.9): each record carries a ``message`` object
  with a ``type`` (TurnBegin, ToolCall, StatusUpdate, CompactionBegin, ...)
  and a ``payload``. Main log at the session root; subagent logs under
  ``subagents/<id>/wire.jsonl``.
- Kimi Desktop format (protocol 1.5): typed records such as ``turn.prompt``,
  ``context.append_loop_event`` (step.begin, content.part, tool.call,
  tool.result), ``token_counting.measured``, ``subagent.spawned``. Main log
  at ``agents/main/wire.jsonl``; subagent logs under ``agents/agent-*/``.
- Codex rollout format: records with top-level types ``session_meta``,
  ``response_item``, ``event_msg``, ``token_usage_record``, ``world_state``,
  ``turn_context``. Sessions live at
  ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl``.
- Qoder transcript format: records with top-level ``type`` of ``user`` or
  ``assistant``, each carrying a ``message.content`` array. Transcripts live
  at ``~/.qoder/projects/<workspace>/<session>.jsonl``; subagent transcripts
  under ``<session>/subagents/<id>.jsonl``.

All formats are translated into the same Event model, so downstream modules
only see one shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional


@dataclass
class Event:
    ts: Optional[float]
    type: str
    payload: dict
    origin: str  # "main" or "subagent:<id>"
    source: Path
    seq: int
    # Identity of the source record this event was parsed from: uuid,
    # parent_uuid, is_sidechain, request_id, request_hash, response_hash.
    # Provenance rather than content, so it stays out of ``payload``; ``None``
    # for formats whose logs carry no per-record identity.
    ref: Optional[dict] = None

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
    # Wire-level format id when detection found a distinct shape ("qoder-cli"
    # for the terminal-CLI transcript, which loads degraded). None for the
    # default formats.
    source_format: Optional[str] = None
    # Qoder only: meta.json sidecar per subagent file, keyed by the subagent
    # origin ("subagent:<stem>"). Malformed or missing files are counted as
    # RecordDropped events, never guessed.
    subagent_meta: Dict[str, dict] = field(default_factory=dict)
    # Qoder IDE only: attach stats from the SharedClientCache subagent-chain
    # side-channel (None when the opt-in flag is off).
    ide_db_stats: Optional[dict] = None

    def turns(self) -> List[Event]:
        return [e for e in self.events if e.type == "TurnBegin"]


# ---------------------------------------------------------------------------
# Kimi Desktop format translation
# ---------------------------------------------------------------------------

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
            "input_other": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "input_cache_read": usage.get("cache_read_input_tokens", 0),
            "input_cache_creation": usage.get("cache_creation_input_tokens", 0),
            "model": record.get("model", ""),
        }
    if rtype == "subagent.spawned":
        return "SubagentSpawned", {
            "agent_id": record.get("agentId"),
            "parent_turn": record.get("turnId"),
        }
    return None


def _is_desktop_record(record: dict) -> bool:
    """Heuristic: desktop records have a top-level ``type`` that is a dotted
    namespace (e.g. ``turn.prompt``) or one of the known desktop types."""
    rtype = record.get("type", "")
    return "." in rtype or rtype in (
        "usage.record", "subagent.spawned",
    )


# ---------------------------------------------------------------------------
# Kimi CLI format parser
# ---------------------------------------------------------------------------

def parse_wire(path: Path, origin: str = "main") -> Iterator[Event]:
    """Parse a Kimi wire.jsonl file (CLI or desktop format) into Events."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for seq, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            # Desktop format detection
            if _is_desktop_record(record):
                translated = _translate_desktop(record)
                if translated is None:
                    continue
                etype, payload = translated
                ts = record.get("timestamp")
                yield Event(ts, etype, payload, origin, path, seq)
                continue

            # CLI format
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            ts = record.get("timestamp") or message.get("timestamp")
            msg_type = message.get("type")
            payload = message.get("payload") or {}

            if msg_type == "TurnBegin":
                yield Event(ts, "TurnBegin", payload, origin, path, seq)
            elif msg_type == "ToolCall":
                yield Event(ts, "ToolCall", payload, origin, path, seq)
            elif msg_type == "ToolResult":
                yield Event(ts, "ToolResult", payload, origin, path, seq)
            elif msg_type == "ContentPart":
                yield Event(ts, "ContentPart", payload, origin, path, seq)
            elif msg_type == "StatusUpdate":
                yield Event(ts, "StatusUpdate", payload, origin, path, seq)
            elif msg_type == "CompactionBegin":
                yield Event(ts, "CompactionBegin", payload, origin, path, seq)
            elif msg_type == "CompactionEnd":
                yield Event(ts, "CompactionEnd", payload, origin, path, seq)
            elif msg_type == "LLMRequest":
                yield Event(ts, "LLMRequest", payload, origin, path, seq)
            elif msg_type == "UsageRecord":
                yield Event(ts, "UsageRecord", payload, origin, path, seq)
            elif msg_type == "StepBegin":
                yield Event(ts, "StepBegin", payload, origin, path, seq)


# ---------------------------------------------------------------------------
# Codex rollout format parser
# ---------------------------------------------------------------------------

def _codex_timestamp(value: object) -> Optional[float]:
    """Parse a Codex ISO-8601 timestamp to Unix seconds."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _is_codex_record(record: dict) -> bool:
    """Heuristic: Codex records have a top-level ``type`` of ``session_meta``,
    ``response_item``, ``event_msg``, ``token_usage_record``, ``world_state``,
    or ``turn_context``."""
    return record.get("type") in (
        "session_meta", "response_item", "event_msg",
        "token_usage_record", "world_state", "turn_context",
    )


def parse_codex_session(path: Path, origin: str = "main") -> Iterator[Event]:
    """Parse a Codex rollout JSONL session into Events.

    Codex sessions live at ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl``.
    Record types:
    - ``session_meta``: session metadata (skip, but note cwd/model)
    - ``event_msg``: task_started, token_count, turn_completed (structural, skip)
    - ``response_item``: message, reasoning, function_call, function_call_output
    - ``world_state``: environment snapshot (skip)
    - ``turn_context``: per-turn context (skip)
    - ``token_usage_record``: per-turn token usage
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for seq, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            rtype = record.get("type")
            payload = record.get("payload") or {}
            ts = _codex_timestamp(record.get("timestamp"))

            if rtype == "response_item":
                pt = payload.get("type")
                if pt == "message":
                    role = payload.get("role", "")
                    content = payload.get("content") or []
                    if role == "user":
                        user_input = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "input_text":
                                user_input.append({"type": "text", "text": part.get("text", "")})
                        if user_input:
                            yield Event(ts, "TurnBegin", {"user_input": user_input}, origin, path, seq)
                    elif role == "assistant":
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "output_text":
                                yield Event(ts, "ContentPart", {"type": "text", "text": part.get("text", "")}, origin, path, seq)
                    # role=developer is system instructions — skip
                elif pt == "reasoning":
                    summary = payload.get("summary") or []
                    for part in summary:
                        if isinstance(part, dict) and part.get("type") == "summary_text":
                            yield Event(ts, "ContentPart", {"type": "think", "think": part.get("text", "")}, origin, path, seq)
                            break  # only first summary_text
                elif pt == "function_call":
                    yield Event(ts, "ToolCall", {
                        "id": payload.get("call_id"),
                        "function": {
                            "name": payload.get("name"),
                            "arguments": payload.get("arguments", "{}"),
                        },
                    }, origin, path, seq)
                elif pt == "function_call_output":
                    yield Event(ts, "ToolResult", {
                        "tool_call_id": payload.get("call_id"),
                        "return_value": {
                            "output": payload.get("output", ""),
                            "is_error": False,
                        },
                    }, origin, path, seq)

            elif rtype == "token_usage_record":
                usage = payload.get("usage") or {}
                input_tokens = usage.get("input_tokens", 0) or 0
                cached = usage.get("cached_input_tokens", 0) or 0
                yield Event(ts, "UsageRecord", {
                    "input_other": input_tokens - cached,
                    "output": usage.get("output_tokens", 0),
                    "input_cache_read": cached,
                    "input_cache_creation": usage.get("cache_write_input_tokens", 0),
                    "model": "",
                }, origin, path, seq)

            # session_meta, event_msg, world_state, turn_context are skipped


# ---------------------------------------------------------------------------
# Qoder transcript format parser
# ---------------------------------------------------------------------------

def _qoder_timestamp(value: object) -> Optional[float]:
    """Qoder mixes two timestamp encodings in one transcript.

    ``user``/``assistant``/``system``/``attachment`` records use ISO-8601
    strings; ``runtime-config`` and ``active-leaf`` use epoch milliseconds.
    Returning ``None`` for the numeric form would sort those records after
    every other event, since the session sort puts missing timestamps last.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_qoder_record(record: dict) -> bool:
    """Heuristic: Qoder transcript records have a top-level ``type`` of
    ``user``, ``assistant``, or ``metadata``."""
    return record.get("type") in ("user", "assistant", "metadata")


def _qoder_human_text(record: dict) -> Optional[str]:
    """Text that arrived on the human-input channel, verbatim, separate from the
    harness expansion in ``message.content``.

    Channel, not author. ``origin.kind`` is ``"human"`` on every record that
    carries ``humanInput.text``, so it discriminates nothing: the same channel
    also delivers a subagent's opening record, where the text is the parent
    agent's Task brief rather than operator keystrokes. Roughly half the
    records corpus-wide are such briefs, and they are separable only by agent
    attribution, which is built later. So this reports the raw fact and
    ``ir.items`` decides whether to stamp it, keeping the top-level
    conversation alone.

    The terminal CLI format writes neither field, so there the typed text is not
    recoverable and this stays None instead of being inferred from the expansion.
    """
    human_input = record.get("humanInput")
    if not isinstance(human_input, dict):
        return None
    text = human_input.get("text")
    return text if isinstance(text, str) else None


def _qoder_ref(record: dict) -> dict:
    """Identity of one transcript record, shared by every event parsed from it.

    ``requestTokenAnchor`` is the preferred per-call identity: it names every
    record of a request, while ``usage`` appears on only about a third of them.
    But Qoder began writing the anchor only in 1.1.58, so older transcripts fall
    back to ``usage.request_id`` and can group just their usage-bearing records.
    """
    anchor = record.get("requestTokenAnchor")
    if not isinstance(anchor, dict):
        anchor = {}
    request_id = anchor.get("requestId")
    if request_id is None:
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            request_id = usage.get("request_id")
    return {
        "uuid": record.get("uuid"),
        "parent_uuid": record.get("parentUuid"),
        "is_sidechain": record.get("isSidechain"),
        "parent_tool_use_id": record.get("parentToolUseId"),
        "request_id": request_id,
        "request_hash": anchor.get("request"),
        "response_hash": anchor.get("response"),
        "is_compact_summary": bool(record.get("isCompactSummary")),
        "human_text": _qoder_human_text(record),
    }


def _qoder_compaction_event(record: dict, ts: Optional[float], origin: str, path: Path, seq: int) -> Optional[Event]:
    """A ``system``/``compact_boundary`` record carries harness-measured facts.

    Unlike Kimi's CompactionBegin/End pair, Qoder reports one boundary record
    with the pre/post token counts already measured, so no inference is needed.
    """
    if record.get("subtype") != "compact_boundary":
        return None
    metadata = record.get("compactMetadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return Event(
        ts,
        "CompactionBegin",
        {
            "trigger": metadata.get("trigger"),
            "pre_tokens": metadata.get("preTokens"),
            "post_tokens": metadata.get("postTokens"),
            "messages_summarized": metadata.get("messagesSummarized"),
            "duration_ms": metadata.get("durationMs"),
            "logical_parent_uuid": record.get("logicalParentUuid"),
        },
        origin,
        path,
        seq,
        _qoder_ref(record),
    )


def _qoder_meta_event(record: dict, origin: str, path: Path, seq: int) -> Optional[Event]:
    """Translate a record that carries no ``message`` into an Event.

    These are the records the ``user``/``assistant`` filter used to discard.
    They hold the facts the audit IR is built on: the compaction boundary with
    harness-measured token counts, the runtime context window, the
    conversation-tree leaf, and the attachment channel that carries skill
    catalogs, full skill bodies, reminders and hook output.
    """
    record_type = record.get("type")
    ts = _qoder_timestamp(record.get("timestamp"))
    ref = _qoder_ref(record)
    if record_type == "system":
        compaction = _qoder_compaction_event(record, ts, origin, path, seq)
        if compaction is not None:
            return compaction
        # Qoder's other ``system`` subtypes are harness log lines, not prompt
        # content: ``informational`` (goal set, notices) and ``api_retry``
        # (a request that produced nothing and was retried). Neither belongs in
        # a content bucket, but both are session facts worth keeping.
        return Event(ts, "SystemNotice", {
            "subtype": record.get("subtype"),
            "level": record.get("level"),
            "content": record.get("content"),
            "attempt": record.get("attempt"),
            "max_retries": record.get("max_retries"),
            "retry_delay_ms": record.get("retry_delay_ms"),
            "error": record.get("error"),
            "error_status": record.get("error_status"),
        }, origin, path, seq, ref)
    if record_type == "runtime-config":
        return Event(ts, "RuntimeConfig", {
            "model": record.get("model"),
            "context_window": record.get("contextWindow"),
        }, origin, path, seq, ref)
    if record_type == "active-leaf":
        return Event(ts, "ActiveLeaf", {
            "leaf_uuid": record.get("leafUuid"),
            "explicit": record.get("explicit"),
        }, origin, path, seq, ref)
    if record_type == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict):
            return None
        return Event(ts, "Attachment", {
            "attachment_type": attachment.get("type"),
            "attachment": attachment,
        }, origin, path, seq, ref)
    return None


def parse_qoder_transcript(path: Path, origin: str = "main") -> Iterator[Event]:
    """Parse a Qoder project transcript JSONL into Events.

    Qoder transcripts live at ``~/.qoder/projects/<workspace>/<session>.jsonl``.
    Message records have a top-level ``type`` of ``user`` or ``assistant`` and a
    ``message.content`` array; the remaining record types carry session facts
    and injected content (see :func:`_qoder_meta_event`). Subagent transcripts
    live under ``<session>/subagents/<id>.jsonl``.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for seq, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                yield Event(None, "RecordDropped", {"reason": "malformed_json"}, origin, path, seq, None)
                continue
            if not isinstance(record, dict):
                yield Event(None, "RecordDropped", {"reason": "non_dict"}, origin, path, seq, None)
                continue
            if record.get("type") not in ("user", "assistant"):
                meta = _qoder_meta_event(record, origin, path, seq)
                if meta is not None:
                    yield meta
                else:
                    yield Event(
                        _qoder_timestamp(record.get("timestamp")),
                        "RecordDropped",
                        {"reason": "record:%s" % record.get("type")},
                        origin,
                        path,
                        seq,
                        _qoder_ref(record),
                    )
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                yield Event(
                    _qoder_timestamp(record.get("timestamp")),
                    "RecordDropped",
                    {"reason": "message_invalid"},
                    origin,
                    path,
                    seq,
                    _qoder_ref(record),
                )
                continue
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                yield Event(
                    _qoder_timestamp(record.get("timestamp")),
                    "RecordDropped",
                    {"reason": "content_invalid"},
                    origin,
                    path,
                    seq,
                    _qoder_ref(record),
                )
                continue
            ts = _qoder_timestamp(record.get("timestamp"))
            ref = _qoder_ref(record)
            if record["type"] == "user":
                user_input = [part for part in content if isinstance(part, dict) and part.get("type") == "text"]
                if user_input:
                    yield Event(ts, "TurnBegin", {"user_input": user_input}, origin, path, seq, ref)
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        yield Event(ts, "ToolResult", {
                            "tool_call_id": part.get("tool_use_id"),
                            "return_value": {
                                "output": part.get("content", ""),
                                "is_error": bool(part.get("is_error", False)),
                            },
                        }, origin, path, seq, ref)
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    yield Event(ts, "ContentPart", {"type": "text", "text": part.get("text", "")}, origin, path, seq, ref)
                elif part.get("type") == "thinking":
                    yield Event(ts, "ContentPart", {"type": "think", "think": part.get("thinking", "")}, origin, path, seq, ref)
                elif part.get("type") == "tool_use":
                    yield Event(ts, "ToolCall", {
                        "id": part.get("id"),
                        "function": {
                            "name": part.get("name"),
                            "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False),
                        },
                    }, origin, path, seq, ref)
            usage = message.get("usage")
            if isinstance(usage, dict):
                cache_creation = usage.get("cache_creation")
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                yield Event(ts, "UsageRecord", {
                    "input_other": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "input_cache_read": usage.get("cache_read_input_tokens", 0),
                    "input_cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "input_cache_creation_5m": cache_creation.get("ephemeral_5m_input_tokens", 0),
                    "input_cache_creation_1h": cache_creation.get("ephemeral_1h_input_tokens", 0),
                    "model": message.get("model", record.get("model", "")),
                    "context_usage_ratio": usage.get("context_usage_ratio"),
                    "request_id": usage.get("request_id"),
                    "credits": usage.get("credits"),
                    "original_credits": usage.get("original_credits"),
                    "billable": usage.get("billable"),
                }, origin, path, seq, ref)


# ---------------------------------------------------------------------------
# Format detection and session loading
# ---------------------------------------------------------------------------

def _detect_format(path: Path) -> Optional[str]:
    """Detect the format of a session file by scanning its records.

    Returns "codex", "qoder", "qoder-cli", "kimi", or None if undetectable.

    Qoder terminal CLI transcripts (both variants: with a session_meta
    leader carrying a ``data`` dict, and the older envelope-less records
    interleaved with ``progress`` records) are detected as "qoder-cli" so
    load_session loads them degraded — they carry no usage telemetry. Rich
    IDE records always carry the parentUuid/origin envelope; CLI records
    never do, so an envelope-less user/assistant record keeps the scan going
    until a decisive marker appears.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                # Both codex rollouts and Qoder terminal CLI transcripts can
                # lead with a session_meta record; codex carries a "payload"
                # dict, the terminal CLI a "data" dict.
                if rtype == "session_meta" and isinstance(record.get("data"), dict):
                    return "qoder-cli"
                # The terminal CLI interleaves progress records (hook runs);
                # rich IDE transcripts never contain them.
                if rtype == "progress":
                    return "qoder-cli"
                if _is_codex_record(record):
                    return "codex"
                if _is_qoder_record(record):
                    if "origin" in record or "parentUuid" in record:
                        return "qoder"
                    # Envelope-less message record: ambiguous between an old
                    # CLI transcript and a rare rich record — keep scanning.
                    continue
                # Kimi CLI format has a "message" key with "type" inside
                if "message" in record and isinstance(record.get("message"), dict):
                    return "kimi"
                # Kimi desktop format has dotted types
                if "." in rtype:
                    return "kimi"
                # Header records (workspace-directories, runtime-config, ...)
                # match nothing — keep scanning.
                continue
    except (OSError, IOError):
        return None
    return None


def load_session(directory: Path) -> Session:
    """Load a session from a directory or file path.

    Supports:
    - Kimi session directory (with wire.jsonl or agents/main/wire.jsonl)
    - Qoder transcript file (.jsonl)
    - Qoder terminal CLI transcript (<workspace>/transcript/<uuid>.jsonl):
      loads degraded (source_format "qoder-cli", no compaction telemetry)
      because it carries no usage telemetry.
    - Codex rollout file (.jsonl)
    """
    directory = Path(directory)

    # Single file input: detect format
    if directory.is_file():
        fmt = _detect_format(directory)
        if fmt == "qoder-cli":
            # <workspace>/transcript/<uuid>.jsonl from the Qoder terminal
            # CLI: no envelope, no usage telemetry. The conversation and
            # tool calls still reconstruct; the session is stamped
            # degraded so token costs stay honestly unavailable downstream.
            session = Session(directory=directory.parent,
                              compaction_telemetry_available=False,
                              source_format="qoder-cli")
            session.events.extend(parse_qoder_transcript(directory))
            session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
            return session
        if fmt == "codex":
            session = Session(directory=directory.parent, compaction_telemetry_available=False)
            session.events.extend(parse_codex_session(directory))
            # Look for Codex subagent sessions in the same directory
            # (Codex doesn't have subagent files in the same way, but we
            # keep the structure for future extension)
            session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
            return session
        else:
            # Default to Qoder transcript format. Qoder records every compaction
            # as a system/compact_boundary record with harness-measured token
            # counts, so the telemetry is available rather than inferred.
            session = Session(directory=directory.parent, compaction_telemetry_available=True)
            session.events.extend(parse_qoder_transcript(directory))
            # Look for Qoder subagent transcripts in two possible locations:
            # 1. <session_dir>/<session_stem>/subagents/*.jsonl
            # 2. <session_dir>/subagents/*.jsonl (if session_dir is the session itself)
            sub_dir1 = directory.parent / directory.stem / "subagents"
            sub_dir2 = directory.parent / "subagents"
            for sub_dir in [sub_dir1, sub_dir2]:
                if sub_dir.is_dir():
                    for sub_file in sorted(sub_dir.glob("*.jsonl")):
                        sub_origin = "subagent:%s" % sub_file.stem
                        session.events.extend(parse_qoder_transcript(sub_file, origin=sub_origin))
                        # meta.json sidecar: harness-written spawn facts
                        # (toolUseId, invocationName, ...). Missing or
                        # malformed files are counted, never guessed.
                        meta_path = sub_file.with_suffix(".meta.json")
                        if not meta_path.exists():
                            session.events.append(Event(
                                None, "RecordDropped",
                                {"reason": "meta_json_missing"},
                                sub_origin, meta_path, 1, None,
                            ))
                            continue
                        try:
                            with open(meta_path, "r", encoding="utf-8") as meta_fh:
                                meta = json.load(meta_fh)
                        except (OSError, IOError, json.JSONDecodeError, ValueError):
                            session.events.append(Event(
                                None, "RecordDropped",
                                {"reason": "meta_json_malformed"},
                                sub_origin, meta_path, 1, None,
                            ))
                            continue
                        if isinstance(meta, dict):
                            session.subagent_meta[sub_origin] = meta
                        else:
                            session.events.append(Event(
                                None, "RecordDropped",
                                {"reason": "meta_json_malformed"},
                                sub_origin, meta_path, 1, None,
                            ))
            session.events.sort(key=lambda e: (e.ts is None, e.ts or 0.0, e.seq))
            return session

    # Directory input: Kimi session
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


def find_latest_codex_session(base: Path) -> Optional[Path]:
    """Most recently modified Codex rollout session under the sessions root.

    Codex sessions live at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.
    """
    sessions = list(base.glob("*/*/*/*.jsonl"))
    return max(sessions, key=lambda p: p.stat().st_mtime) if sessions else None


def find_latest_session(base: Path) -> Optional[Path]:
    """Most recently modified session directory under the sessions root."""
    wires = list(base.glob("*/*/wire.jsonl")) + list(base.glob("*/*/agents/main/wire.jsonl"))
    if not wires:
        return None
    newest = max(wires, key=lambda p: p.stat().st_mtime)
    # CLI layout: <root>/<ws>/<session>/wire.jsonl — session is the wire's parent.
    # Agents layout: <root>/<ws>/<session>/agents/main/wire.jsonl — three levels up.
    return newest.parents[2] if newest.parent.name in ("main", "agent") else newest
