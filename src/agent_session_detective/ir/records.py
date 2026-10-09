# src/agent_session_detective/ir/records.py
"""Wire events -> WireRecord (one JSONL line), agent attribution, tier detection.

``load_session`` sorts events by timestamp; the IR needs per-file line order,
so ``group_records`` re-sorts by ``(source, seq, first-seen index)`` and merges
consecutive events with the same ``(source, seq)``. This restores the file's
own line order even when timestamps collide or are missing.

Agent attribution: subagent files announce themselves in the event origin
("subagent:<stem>"); sidechain records are identified by walking the
``uuid``/``parentUuid`` chain to the last non-sidechain ancestor — the chain
root's uuid names the sidechain agent ("sidechain:<root-uuid>"). The walk is
iterative and memoized and breaks on revisits, so malformed chains (dangling
parents, parentUuid cycles) terminate deterministically: the sidechain is
named by the last record reached by the walk, and only a uuid-less record
yields "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..wire import Event

POSITIVE_USAGE_KEYS = (
    "input_other",
    "input_cache_read",
    "input_cache_creation",
    "input_cache_creation_5m",
    "input_cache_creation_1h",
)
ASSISTANT_EVENT_TYPES = ("ContentPart", "ToolCall", "UsageRecord")
USER_EVENT_TYPES = ("TurnBegin", "ToolResult")


@dataclass
class WireRecord:
    source: str
    seq: int
    ts: Optional[float]
    events: List[Event] = field(default_factory=list)

    @property
    def kind(self) -> str:
        types = {event.type for event in self.events}
        if types & set(ASSISTANT_EVENT_TYPES):
            return "assistant"
        if types & set(USER_EVENT_TYPES):
            return "user"
        return "meta"

    @property
    def ref(self) -> Optional[dict]:
        for event in self.events:
            if event.ref is not None:
                return event.ref
        return None

    @property
    def usage(self) -> Optional[dict]:
        for event in self.events:
            if event.type == "UsageRecord":
                return event.payload
        return None

    @property
    def has_positive_usage(self) -> bool:
        usage = self.usage
        if usage is None:
            return False
        return any((usage.get(key) or 0) > 0 for key in POSITIVE_USAGE_KEYS)

    @property
    def uuid(self) -> Optional[str]:
        return (self.ref or {}).get("uuid")

    @property
    def parent_uuid(self) -> Optional[str]:
        return (self.ref or {}).get("parent_uuid")

    @property
    def is_sidechain(self) -> bool:
        return bool((self.ref or {}).get("is_sidechain"))

    @property
    def request_id(self) -> Optional[str]:
        return (self.ref or {}).get("request_id")

    @property
    def request_hash(self) -> Optional[str]:
        return (self.ref or {}).get("request_hash")

    @property
    def response_hash(self) -> Optional[str]:
        return (self.ref or {}).get("response_hash")

    @property
    def human_text(self) -> Optional[str]:
        return (self.ref or {}).get("human_text")

    @property
    def preview_text(self) -> str:
        for event in self.events:
            if event.type == "ContentPart" and event.payload.get("type") == "text":
                return event.payload.get("text", "")
        return ""


@dataclass
class AgentRecords:
    agent_id: str
    origin_channel: str
    parent_id: Optional[str]
    records: List[WireRecord]


def group_records(events) -> List[WireRecord]:
    ordered = sorted(
        enumerate(events),
        key=lambda pair: (str(pair[1].source), pair[1].seq, pair[0]),
    )
    groups: List[WireRecord] = []
    for _, event in ordered:
        key = (str(event.source), event.seq)
        if groups and (groups[-1].source, groups[-1].seq) == key:
            groups[-1].events.append(event)
        else:
            groups.append(WireRecord(key[0], key[1], event.ts, [event]))
    return groups


def _sidechain_root(
    index: Dict[Tuple[str, str], WireRecord],
    memo: Dict[Tuple[str, str], str],
    record: WireRecord,
) -> str:
    if record.uuid is None:
        return "unknown"
    chain: List[WireRecord] = []
    seen: set = set()
    root: Optional[str] = None
    current: Optional[WireRecord] = record
    while current is not None and current.is_sidechain:
        if current.uuid is None:
            break
        key = (current.source, current.uuid)
        cached = memo.get(key)
        if cached is not None:
            root = cached
            break
        if key in seen:
            break
        seen.add(key)
        chain.append(current)
        current = index.get((current.source, current.parent_uuid))
    if root is None:
        root = chain[-1].uuid if chain else "unknown"
    for member in chain:
        memo[(member.source, member.uuid)] = root
    return root


def attribute_agents(records: List[WireRecord]) -> List[AgentRecords]:
    main: List[WireRecord] = []
    sidechains: Dict[str, List[WireRecord]] = {}
    subagents: Dict[str, List[WireRecord]] = {}
    index: Dict[Tuple[str, str], WireRecord] = {}
    for record in records:
        if record.uuid is not None:
            index.setdefault((record.source, record.uuid), record)
    memo: Dict[Tuple[str, str], str] = {}
    for record in records:
        origin = record.events[0].origin if record.events else ""
        if origin.startswith("subagent:"):
            subagents.setdefault(origin, []).append(record)
        elif record.is_sidechain:
            root = _sidechain_root(index, memo, record)
            sidechains.setdefault("sidechain:%s" % root, []).append(record)
        else:
            main.append(record)
    groups: List[AgentRecords] = []
    if main:
        groups.append(AgentRecords("main", "qoder:main", None, main))
    for agent_id in sorted(
        sidechains, key=lambda aid: sidechains[aid][0].seq if sidechains[aid] else 0
    ):
        groups.append(AgentRecords(agent_id, "qoder:isSidechain", "main", sidechains[agent_id]))
    for agent_id in sorted(subagents):
        groups.append(
            AgentRecords(agent_id, "qoder:subagent-file", "main", subagents[agent_id])
        )
    return groups


def detect_tier(records: List[WireRecord]) -> int:
    for record in records:
        if record.request_hash is not None:
            return 1
    for record in records:
        if record.request_id is not None:
            return 2
    return 3
