"""Detect skill loads in a session and build the fact timeline.

Facts come from the log only: a Skill tool call with its result, the status
updates that bracket it, and compaction events. Whether a skill survived a
compaction is not recorded anywhere, so eviction is a labeled inference,
never a fact (R5).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .wire import Event, Session

SKILL_TOOL_NAMES = {"skill", "skill_loaded"}
SKILL_FILE_RE = re.compile(r"SKILL\.md", re.IGNORECASE)
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """Rough token estimate: CJK characters are ~1 token each, others ~4 chars."""
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, cjk + other // 4)


@dataclass
class SkillLoad:
    skill_name: str
    args: str
    ts: Optional[float]
    origin: str
    content: str  # captured at load time (R10)
    is_error: bool
    source_line: int
    tokens_est: int = 0
    context_tokens_after: Optional[int] = None
    context_usage_after: Optional[float] = None
    evicted_by: Optional[int] = None  # index into timeline.compactions

    @property
    def evicted(self) -> bool:
        return self.evicted_by is not None


@dataclass
class SkillFileRead:
    """A SKILL.md read as a plain file — a fact, distinct from a formal load."""

    path: str
    skill_name: str  # guessed from the file's parent directory
    ts: Optional[float]
    origin: str
    source_line: int
    snippet: str


@dataclass
class Compaction:
    index: int
    begin_ts: Optional[float]
    end_ts: Optional[float]


@dataclass
class Timeline:
    loads: List[SkillLoad] = field(default_factory=list)
    file_reads: List[SkillFileRead] = field(default_factory=list)
    compactions: List[Compaction] = field(default_factory=list)
    status_series: List[Event] = field(default_factory=list)
    turns: List[Event] = field(default_factory=list)

    def skill_names(self) -> List[str]:
        return [l.skill_name for l in self.loads]

    def consumed_skill_names(self) -> List[str]:
        """Skills consumed either formally (load) or as plain file reads."""
        return sorted(set(self.skill_names()) | {f.skill_name for f in self.file_reads})


def _skill_name_from_call(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
        return str(parsed.get("skill") or parsed.get("name") or "?")
    except (json.JSONDecodeError, TypeError):
        return "?"


def _result_text(event: Event) -> tuple:
    rv = event.payload.get("return_value") or {}
    return bool(rv.get("is_error")), str(rv.get("output") or "")


def _skill_file_path(arguments: str) -> Optional[str]:
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return None
    path = str(parsed.get("path") or parsed.get("file_path") or "")
    return path if SKILL_FILE_RE.search(path) else None


def build_timeline(session: Session) -> Timeline:
    timeline = Timeline(turns=session.turns())

    results_by_id: Dict[str, Event] = {}
    calls: List[Event] = []
    file_read_calls: List[Event] = []
    for event in session.events:
        if event.type == "ToolCall":
            fn = event.payload.get("function", {})
            name = str(fn.get("name", "")).lower()
            if name in SKILL_TOOL_NAMES:
                calls.append(event)
            elif name == "read":
                path = _skill_file_path(str(fn.get("arguments", "")))
                if path:
                    file_read_calls.append(event)
        elif event.type == "ToolResult":
            results_by_id[str(event.payload.get("tool_call_id"))] = event
        elif event.type == "StatusUpdate":
            timeline.status_series.append(event)
        elif event.type == "CompactionBegin":
            timeline.compactions.append(
                Compaction(index=len(timeline.compactions), begin_ts=event.ts, end_ts=None)
            )
        elif event.type == "CompactionEnd" and timeline.compactions:
            timeline.compactions[-1].end_ts = event.ts

    for call in calls:
        fn = call.payload.get("function", {})
        result = results_by_id.get(str(call.payload.get("id")))
        is_error, content = _result_text(result) if result else (True, "")
        load = SkillLoad(
            skill_name=_skill_name_from_call(str(fn.get("arguments", ""))),
            args=str(fn.get("arguments", "")),
            ts=call.ts,
            origin=call.origin,
            content=content,
            is_error=is_error,
            source_line=call.seq,
            tokens_est=estimate_tokens(content),
        )
        # Nearest status update at or after the load gives context occupancy.
        for status in timeline.status_series:
            if (status.ts or 0) >= (call.ts or 0):
                load.context_tokens_after = status.payload.get("context_tokens")
                load.context_usage_after = status.payload.get("context_usage")
                break
        timeline.loads.append(load)

    for call in file_read_calls:
        fn = call.payload.get("function", {})
        path = _skill_file_path(str(fn.get("arguments", ""))) or "?"
        result = results_by_id.get(str(call.payload.get("id")))
        _, snippet = _result_text(result) if result else (False, "")
        timeline.file_reads.append(
            SkillFileRead(
                path=path,
                skill_name=Path(path).parent.name,
                ts=call.ts,
                origin=call.origin,
                source_line=call.seq,
                snippet=snippet,
            )
        )

    # A compaction plausibly evicts every skill loaded before it. The log
    # never says "skill dropped", so this is a labeled inference (R3/AE1).
    for load in timeline.loads:
        for compaction in timeline.compactions:
            if load.evicted_by is None and (compaction.begin_ts or 0) >= (load.ts or 0):
                load.evicted_by = compaction.index

    return timeline
