# src/agent_session_detective/ir/dispatch.py
"""Dispatch join, brief classification (D3), phase recognition (IR 1.1).

**The join is the definition**: a dispatch exists when a subagent transcript
links to a parent ``tool_use_id`` by string equality — via
``parent_tool_use_id`` on its records, or via meta.json ``toolUseId`` when
records lack the field (counted ``joined_via_meta_only``). Id formats are
never parsed. Orphan directions are counted, never dropped: a parent ``Agent``
call with no subagent file becomes a row with ``subagent_agent_id: null``
(the name is advisory — a dispatch is never fabricated from a name alone,
only reported), and a subagent file matching no parent tool call counts in
``unmatched_subagent_files`` with no row.

The brief is the first string-content user record of a subagent-origin agent.
A signature brief keeps ``kind: "skill_body"`` (tier A, FACT); any other
brief is re-classified ``kind: "dispatch_brief"`` and re-bucketed
``inject`` (D3 — machine-authored, user-role by protocol only). The mutation
happens here, on the shared ``ContentItem`` objects, and the builder runs it
before span/bucket aggregation so ``Σbuckets + unattributed == anchor`` is
aggregated once, from the final classification.

Phase tiers per dispatch, exclusive: A (brief is a skill body →
``phase_id`` = its ``skill_id``), B (rule matches on the brief text or the
subagent's Read file paths; ``phase_id`` set only when the refs name exactly
one phase), C (no match, orphans included). Orphans count as tier C so
tierA + tierB + tierC == len(dispatches).
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from .phase_rules import CORPUS_NOTE, RULE_SET_VERSION, RULES, match_refs, rule_name
from .records import AgentRecords
from .schema import ContentItem, Dispatch, PhaseEntity, PhaseObservation

SIGNATURE_CHANNEL = "qoder:signature:skill_body"


def _is_subagent(agent_id: str) -> bool:
    return agent_id.startswith("subagent:")


def _agent_items(items: List[ContentItem], agent_id: str) -> List[ContentItem]:
    # extraction.items is built per agent in wire order, so this filter
    # preserves wire order.
    return [item for item in items if item.agent_id == agent_id]


def _brief_text(record_lookup, item: ContentItem) -> str:
    """Full brief text from the wire record (facts), not the 200-char preview."""
    record = record_lookup((item.record.get("file"), item.wire_seq))
    if record is None:
        return item.preview
    for event in record.events:
        if event.type != "TurnBegin":
            continue
        texts = [
            part.get("text")
            for part in event.payload.get("user_input") or []
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(texts)
    return item.preview


def _call_payload(record_lookup, item: ContentItem) -> dict:
    """The tool_call event payload behind ``item`` (full arguments)."""
    record = record_lookup((item.record.get("file"), item.wire_seq))
    if record is None:
        return {}
    for event in record.events:
        if event.type != "ToolCall":
            continue
        if item.tool_use_id is not None and event.payload.get("id") != item.tool_use_id:
            continue
        return event.payload
    return {}


def _parsed_arguments(record_lookup, item: ContentItem) -> dict:
    payload = _call_payload(record_lookup, item)
    function = payload.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _read_file_paths(record_lookup, sub_items: List[ContentItem]) -> List[Tuple[ContentItem, str]]:
    """(Read tool_call item, file_path) pairs of one agent, wire order."""
    reads: List[Tuple[ContentItem, str]] = []
    for item in sub_items:
        if item.kind != "tool_call":
            continue
        if (item.name or "").lower() != "read":
            continue
        arguments = _parsed_arguments(record_lookup, item)
        file_path = arguments.get("file_path")
        if isinstance(file_path, str) and file_path:
            reads.append((item, file_path))
    return reads


def build_dispatches(
    agents: List[AgentRecords],
    items: List[ContentItem],
    subagent_meta: Dict[str, dict],
    seq_ts: Dict[Tuple[str, int], Optional[float]],
    rules: List[dict] = RULES,
    rule_set_version: Optional[str] = RULE_SET_VERSION,
) -> Tuple[List[Dispatch], List[PhaseEntity], dict, dict]:
    """Join subagent transcripts to parent Agent calls; classify briefs (D3);
    extract phase refs. Returns ``(dispatches, phases, dispatch_links,
    phase_recognition)`` — the two dicts land in ``CoverageReport`` verbatim.

    Must run before span/bucket aggregation (D3 mutates items here).
    """
    # Parent-side tool calls keyed by tool_use_id (string equality only).
    parent_by_tool_id: Dict[str, ContentItem] = {}
    for item in items:
        if item.kind == "tool_call" and item.tool_use_id is not None:
            parent_by_tool_id.setdefault(item.tool_use_id, item)

    records_by_key: Dict[Tuple[str, int], object] = {}

    def record_lookup(key):
        return records_by_key.get(key)

    for agent in agents:
        for record in agent.records:
            records_by_key[(record.source, record.seq)] = record

    # --- Brief classification + D3 re-bucket (every subagent-origin agent,
    # joined or not: the bucket honesty does not depend on the join). ---
    briefs: Dict[str, Optional[ContentItem]] = {}
    sub_reads: Dict[str, List[Tuple[ContentItem, str]]] = {}
    for agent in agents:
        if not _is_subagent(agent.agent_id):
            continue
        sub_items = _agent_items(items, agent.agent_id)
        brief: Optional[ContentItem] = None
        for item in sub_items:
            if item.kind == "user_message" or (
                item.kind == "skill_body" and item.channel == SIGNATURE_CHANNEL
            ):
                brief = item
                break
        briefs[agent.agent_id] = brief
        if brief is not None and brief.kind == "user_message":
            brief.kind = "dispatch_brief"
            brief.bucket = "inject"
        sub_reads[agent.agent_id] = _read_file_paths(record_lookup, sub_items)

    # --- The join. ---
    claimed: Dict[str, str] = {}  # parent tool_use_id -> first claiming subagent
    candidates: List[dict] = []  # joined rows, pre-ordering
    unmatched_subagent_files = 0
    for agent in agents:
        if not _is_subagent(agent.agent_id):
            continue
        link_id: Optional[str] = None
        link_source: Optional[str] = None
        for record in agent.records:
            value = (record.ref or {}).get("parent_tool_use_id")
            if value is not None:
                link_id = value
                link_source = "records"
                break
        if link_id is None:
            meta = subagent_meta.get(agent.agent_id)
            if isinstance(meta, dict):
                value = meta.get("toolUseId")
                if isinstance(value, str) and value:
                    link_id = value
                    link_source = "meta"
        if link_id is None or link_id not in parent_by_tool_id:
            unmatched_subagent_files += 1
            continue
        claimed.setdefault(link_id, agent.agent_id)
        candidates.append({
            "subagent_agent_id": agent.agent_id,
            "parent_item": parent_by_tool_id[link_id],
            "link_source": link_source,
        })

    # --- Orphan parent Agent calls (advisory name, unclaimed id). ---
    orphan_items: List[ContentItem] = []
    for item in items:
        if item.kind != "tool_call" or item.tool_use_id is None:
            continue
        if item.tool_use_id in claimed:
            continue
        if (item.name or "").lower() != "agent":
            continue
        orphan_items.append(item)

    # --- Per-parent-agent row emission, parent wire order. ---
    agent_order = [agent.agent_id for agent in agents]
    per_parent: Dict[str, List[dict]] = {}
    for entry in candidates:
        per_parent.setdefault(entry["parent_item"].agent_id, []).append({
            **entry,
            "orphan": None,
        })
    for item in orphan_items:
        per_parent.setdefault(item.agent_id, []).append({
            "subagent_agent_id": None,
            "parent_item": item,
            "link_source": None,
            "orphan": item,
        })
    for parent_rows in per_parent.values():
        parent_rows.sort(
            key=lambda row: (row["parent_item"].wire_seq, row["parent_item"].item_id)
        )

    dispatches: List[Dispatch] = []
    phases: List[PhaseEntity] = []
    tier_counts = {"A": 0, "B": 0, "C": 0}
    briefs_found = 0
    joined = 0
    joined_via_ide_db = 0
    joined_via_meta_only = 0
    orphan_dispatches = 0

    for parent_agent_id in agent_order:
        rows = per_parent.get(parent_agent_id, [])
        for n, row in enumerate(rows, start=1):
            parent_item: ContentItem = row["parent_item"]
            subagent_agent_id: Optional[str] = row["subagent_agent_id"]
            dispatch_id = "%s:dispatch:%d" % (parent_agent_id, n)
            arguments = _parsed_arguments(record_lookup, parent_item)
            meta: Optional[dict] = None
            subagent_type = arguments.get("subagent_type")
            description = arguments.get("description")
            brief: Optional[ContentItem] = None
            if subagent_agent_id is not None:
                if row["link_source"] == "records":
                    if subagent_agent_id.startswith("subagent:ide-db:"):
                        joined_via_ide_db += 1
                    else:
                        joined += 1
                else:
                    joined_via_meta_only += 1
                meta = subagent_meta.get(subagent_agent_id)
                if not isinstance(meta, dict):
                    meta = None
                if not isinstance(subagent_type, str) and isinstance(meta, dict):
                    subagent_type = meta.get("agentType")
                if not isinstance(description, str) and isinstance(meta, dict):
                    description = meta.get("description")
                brief = briefs.get(subagent_agent_id)
            else:
                orphan_dispatches += 1

            brief_item_id = None
            brief_tokens_est = None
            brief_channel = None
            refs: List[dict] = []
            tier = "C"
            phase_id: Optional[str] = None
            read_path_to_item: Dict[str, ContentItem] = {}
            if subagent_agent_id is not None:
                if brief is not None:
                    briefs_found += 1
                    brief_item_id = brief.item_id
                    brief_tokens_est = brief.tokens_est
                    brief_channel = brief.channel
                if brief is not None and brief.kind == "skill_body":
                    # Tier A: the brief is the body — FACT recognition.
                    tier = "A"
                    phase_id = brief.skill_id
                else:
                    # Tier B: rules over brief text and Read file paths.
                    if brief is not None:
                        refs.extend(match_refs(
                            rules, _brief_text(record_lookup, brief),
                            "brief", "brief"))
                    for read_item, file_path in sub_reads.get(subagent_agent_id, []):
                        refs.extend(match_refs(
                            rules, file_path, "read:%s" % file_path, "read"))
                        read_path_to_item[file_path] = read_item
                    phase_keys = sorted({
                        ref["phase_id"] for ref in refs if ref.get("phase_id")
                    })
                    if len(phase_keys) == 1:
                        tier = "B"
                        phase_id = phase_keys[0]
                    elif len(phase_keys) > 1:
                        # Multi-ref: evidence recorded, attribution withheld
                        # (phase_id null) — never guessed between phases.
                        tier = "B"
            else:
                tier = "C"

            # Phase stamping (D1-style link): only with a resolved phase.
            if phase_id is not None:
                if brief is not None:
                    brief.phase_id = phase_id
                if tier == "B":
                    for ref in refs:
                        if ref.get("phase_id") != phase_id:
                            continue
                        if ref["source"].startswith("read:"):
                            read_item = read_path_to_item.get(
                                ref["source"][len("read:"):])
                            if read_item is not None:
                                read_item.phase_id = phase_id

            dispatches.append(Dispatch(
                dispatch_id=dispatch_id,
                parent_agent_id=parent_agent_id,
                tool_use_id=parent_item.tool_use_id or "",
                tool_item_id=parent_item.item_id,
                subagent_agent_id=subagent_agent_id,
                brief_item_id=brief_item_id,
                brief_tokens_est=brief_tokens_est,
                subagent_type=subagent_type if isinstance(subagent_type, str) else None,
                description=description if isinstance(description, str) else None,
                meta=meta,
                phase_refs=refs,
                phase_id=phase_id,
            ))
            tier_counts[tier] += 1

            # PhaseEntity: evidence-backed phases only (A always; B per
            # matched phase, single- or multi-ref).
            if tier == "A" and brief is not None:
                phases.append(PhaseEntity(
                    phase_id=phase_id,
                    name=brief.name,
                    observations=[PhaseObservation(
                        kind="dispatch",
                        agent_id=parent_agent_id,
                        dispatch_id=dispatch_id,
                        ts=seq_ts.get((parent_item.record.get("file"),
                                      parent_item.wire_seq)),
                        tokens_est=brief_tokens_est,
                        channel=brief_channel,
                    )],
                ))
            elif tier == "B" and refs:
                phase_keys = sorted({
                    ref["phase_id"] for ref in refs if ref.get("phase_id")
                })
                dispatch_ts = seq_ts.get((parent_item.record.get("file"),
                                          parent_item.wire_seq))
                for key in phase_keys:
                    phases.append(PhaseEntity(
                        phase_id=key,
                        name=rule_name(rules, key),
                        observations=[PhaseObservation(
                            kind="dispatch",
                            agent_id=parent_agent_id,
                            dispatch_id=dispatch_id,
                            ts=dispatch_ts,
                            # Multi-ref brief: cost withheld — never double
                            # counted across phases.
                            tokens_est=brief_tokens_est if len(phase_keys) == 1 else None,
                            channel=brief_channel,
                        )],
                    ))

    dispatch_links = {
        "dispatches": len(dispatches),
        "joined": joined,
        "joined_via_ide_db": joined_via_ide_db,
        "joined_via_meta_only": joined_via_meta_only,
        "orphan_dispatches": orphan_dispatches,
        "unmatched_subagent_files": unmatched_subagent_files,
        "meta_files_loaded": len(subagent_meta),
        "briefs_found": briefs_found,
        "briefs_missing": len(dispatches) - briefs_found,
    }
    phase_recognition = {
        "tierA": tier_counts["A"],
        "tierB": tier_counts["B"],
        "tierC": tier_counts["C"],
        "rule_set_version": rule_set_version,
        "corpus_note": CORPUS_NOTE,
    }
    return dispatches, phases, dispatch_links, phase_recognition
