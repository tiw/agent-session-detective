# src/agent_session_detective/ir/loads.py
"""Skill-load evidence ledger (IR 1.1, landing path 1).

One wire-order walk per agent over every ``SkillEntity`` observation of kind
``body``/``stub``, with pairing state (open stub, last attached body) kept
per ``(agent_id, skill_id)`` — the join is id/pairing logic, never re-parsing
of transcript text. The ledger's founding rules:

1. A stub opens a pending load for its skill.
2. The next body of that skill before the next stub attaches to the open
   stub and closes it — first body wins per stub; later bodies are not
   attached to it (they flow through rule 3, so duplicates stay counted).
3. A body with no open pending load becomes a row of its own: first body
   ever for the skill in that agent → ``kind: "load"``; previous attached
   body compacted away (``gone_seq`` set) → ``kind: "reload"``; previous
   body still alive → **no row**, counted in ``redundant_bodies`` (the
   comparison base does not move — the still-alive body stays the reference).
4. A pending stub unattached when its successor stub arrives, or at walk
   end → ``unavailable`` row: ``cost_tokens_est: null``, ``channel: null``,
   marker kept so the gap stays inspectable. A stub-derived number is never
   produced.

Cost is always the attached body item's ``tokens_est`` (EST). Read-loaded
bodies (channel ``tool:read``) participate like any other body. Rows are
stored per agent in wire order; ``load_id`` numbering follows that order.
Reads only the document.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .schema import ContentItem, LoadEvidence, SkillEntity


class _Entry:
    """One body or stub observation, enriched with item order/liveness."""

    __slots__ = ("kind", "skill_id", "item_id", "ts", "tokens_est", "channel",
                 "body_sha1", "wire_seq", "gone_seq")

    def __init__(self, kind, skill_id, item_id, ts, tokens_est, channel,
                 body_sha1, wire_seq, gone_seq):
        self.kind = kind
        self.skill_id = skill_id
        self.item_id = item_id
        self.ts = ts
        self.tokens_est = tokens_est
        self.channel = channel
        self.body_sha1 = body_sha1
        self.wire_seq = wire_seq
        self.gone_seq = gone_seq


def _collect_entries(
    skills: Sequence[SkillEntity], items: Sequence[ContentItem]
) -> Dict[str, List[_Entry]]:
    """body/stub observations grouped per agent, in wire order."""
    items_by_id = {item.item_id: item for item in items}
    per_agent: Dict[str, List[_Entry]] = {}
    for skill in skills:
        for obs in skill.observations:
            if obs.kind not in ("body", "stub"):
                continue
            item = items_by_id.get(obs.item_id)
            if item is None:
                # The contract test requires every observation item to
                # resolve; an unresolvable reference is a builder bug, not a
                # ledger concern.
                continue
            per_agent.setdefault(obs.agent_id, []).append(_Entry(
                kind=obs.kind,
                skill_id=skill.skill_id,
                item_id=obs.item_id,
                ts=obs.ts,
                tokens_est=obs.tokens_est,
                channel=obs.channel,
                body_sha1=obs.body_sha1,
                wire_seq=item.wire_seq,
                gone_seq=item.gone_seq,
            ))
    for entries in per_agent.values():
        entries.sort(key=lambda entry: (entry.wire_seq, entry.item_id))
    return per_agent


def build_loads(
    skills: Sequence[SkillEntity],
    items: Sequence[ContentItem],
    agents: Sequence,
) -> Tuple[List[LoadEvidence], int]:
    """The load ledger: (rows in per-agent wire order, redundant count).

    Reads only projected IR state — ``SkillEntity`` observations, item
    liveness (``gone_seq``), agent order — never re-parses transcript text.
    Returns ``(loads, redundant_bodies)``; the redundant count feeds the
    coverage ``skill_load_evidence`` block (counted without rows).
    """
    per_agent = _collect_entries(skills, items)
    agent_order = [agent.agent_id for agent in agents]
    # Agents present in observations but absent from document.agents (a
    # hand-built document) keep a deterministic tail order.
    for agent_id in sorted(per_agent):
        if agent_id not in agent_order:
            agent_order.append(agent_id)

    loads: List[LoadEvidence] = []
    redundant_bodies = 0
    for agent_id in agent_order:
        # Each row is keyed by its *opening* event (the stub for stub+body
        # pairs and unavailable rows, the body for body-only rows) so the
        # agent's rows can be emitted in wire order even though unattached
        # stubs are only closed at walk end.
        rows: List[Tuple[Tuple[int, str], dict]] = []
        pending: Dict[str, _Entry] = {}  # skill_id -> open stub marker
        last_body: Dict[str, _Entry] = {}  # skill_id -> last attached body
        for entry in per_agent.get(agent_id, []):
            if entry.kind == "stub":
                open_marker = pending.get(entry.skill_id)
                if open_marker is not None:
                    # Successor stub arrived: close unattached (rule 4).
                    rows.append(((open_marker.wire_seq, open_marker.item_id),
                                 {"skill_id": entry.skill_id,
                                  "marker": open_marker, "body": None}))
                pending[entry.skill_id] = entry
                continue
            # kind == "body"
            marker = pending.pop(entry.skill_id, None)
            previous = last_body.get(entry.skill_id)
            if previous is not None and marker is None:
                if previous.gone_seq is None:
                    redundant_bodies += 1
                    continue  # no row; comparison base stays the alive body
                kind = "reload"  # real re-injection, cost counted
            else:
                # First body ever, or a body attaching to an open stub.
                kind = "load"
            opening = marker if marker is not None else entry
            rows.append(((opening.wire_seq, opening.item_id),
                         {"skill_id": entry.skill_id, "marker": marker,
                          "body": entry, "kind": kind}))
            last_body[entry.skill_id] = entry
        for marker in sorted(pending.values(), key=lambda m: (m.wire_seq, m.item_id)):
            rows.append(((marker.wire_seq, marker.item_id),
                         {"skill_id": marker.skill_id, "marker": marker,
                          "body": None}))

        rows.sort(key=lambda pair: pair[0])
        for n, (_, row) in enumerate(rows, start=1):
            marker: Optional[_Entry] = row["marker"]
            body: Optional[_Entry] = row["body"]
            if body is not None:
                loads.append(LoadEvidence(
                    load_id="%s:load:%d" % (agent_id, n),
                    agent_id=agent_id,
                    skill_id=row["skill_id"],
                    kind=row["kind"],
                    marker_item_id=marker.item_id if marker is not None else None,
                    body_item_id=body.item_id,
                    body_sha1=body.body_sha1,
                    channel=body.channel,
                    ts=marker.ts if marker is not None else body.ts,
                    cost_tokens_est=body.tokens_est,
                    cost_basis="body",
                ))
            else:
                loads.append(LoadEvidence(
                    load_id="%s:load:%d" % (agent_id, n),
                    agent_id=agent_id,
                    skill_id=row["skill_id"],
                    kind="load",
                    marker_item_id=marker.item_id if marker is not None else None,
                    body_item_id=None,
                    body_sha1=None,
                    channel=None,
                    ts=marker.ts if marker is not None else None,
                    cost_tokens_est=None,
                    cost_basis="unavailable",
                ))
    return loads, redundant_bodies
