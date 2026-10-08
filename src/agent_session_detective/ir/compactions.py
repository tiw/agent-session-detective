"""Compaction boundaries: items that died at the boundary and items restored.

Boundary rule (DERIVED): a boundary kills every same-agent item with a
smaller ``wire_seq`` by writing ``gone_seq`` on the item in place; the
earliest boundary wins for items killed by several. The restore window is
the records after a boundary up to, but excluding, the next assistant
record — the summary turn is a user record and does not close the window;
``compact_restore`` items inside it are listed on the ``Compaction``.
"""

from __future__ import annotations

from typing import List, Set

from .records import AgentRecords, WireRecord
from .schema import Compaction, ContentItem


def _assign_gone_sequences(agent_id: str, boundaries: List[int],
                           items: List[ContentItem]) -> None:
    for boundary_seq in sorted(boundaries):
        for item in items:
            if item.agent_id != agent_id or item.gone_seq is not None:
                continue
            if item.wire_seq < boundary_seq:
                item.gone_seq = boundary_seq


def _restored_item_ids(records: List[WireRecord], boundary_seq: int,
                       items: List[ContentItem], agent_id: str) -> List[str]:
    window: Set[int] = set()
    for record in records:
        if record.seq <= boundary_seq:
            continue
        if record.kind == "assistant":
            break
        window.add(record.seq)
    restored = []
    for item in items:
        if item.agent_id != agent_id or item.kind != "compact_restore":
            continue
        if item.wire_seq in window:
            restored.append(item.item_id)
    return restored


def build_compactions(agents: List[AgentRecords],
                      items: List[ContentItem]) -> List[Compaction]:
    compactions: List[Compaction] = []
    for agent in agents:
        boundaries = [
            (record, event)
            for record in agent.records
            for event in record.events
            if event.type == "CompactionBegin"
        ]
        _assign_gone_sequences(
            agent.agent_id, [record.seq for record, _ in boundaries], items
        )
        for index, (record, event) in enumerate(boundaries):
            payload = event.payload
            compactions.append(Compaction(
                compaction_id="%s:compact:%d" % (agent.agent_id, index),
                agent_id=agent.agent_id,
                ts=record.ts,
                boundary_seq=record.seq,
                trigger=payload.get("trigger"),
                pre_tokens=payload.get("pre_tokens"),
                post_tokens=payload.get("post_tokens"),
                messages_summarized=payload.get("messages_summarized"),
                duration_ms=payload.get("duration_ms"),
                restored_item_ids=_restored_item_ids(
                    agent.records, record.seq, items, agent.agent_id
                ),
            ))
    return compactions
