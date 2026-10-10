# src/agent_session_detective/ir/builder.py
"""Project a parsed wire session into an :class:`AuditDocument`.

The builder is the IR's projection boundary: everything above it parses
transcript text (``wire.py``), everything below it reads typed fields. Only
recorded facts are projected — a field the transcript never wrote stays
``None`` and shows up as ``missing`` in coverage, never as a plausible guess.

v1 adapter deviations, documented here so the report layer can state them:

- ``prefix_hashes`` stays ``None`` in v1: the Kimi CLI payload's
  ``system_prompt_hash``/``tools_hash`` reach the wire layer but are not
  projected into IR records, and Qoder records no prefix hash at all.
- Qoder's plain ``input_cache_creation`` counter has no IR slot; only the
  ephemeral 5m/1h breakdowns are projected.
- Zero or missing ``input_other`` means no provider-measured prompt total:
  ``anchor_tokens`` is ``None`` (writing 0 would fabricate a fact).
- ``checked_calls`` counts tier-1 verification only; tier-2 spans are derived
  positionally and have nothing to verify against.
"""

from typing import Dict, List, Optional, Tuple

from ..wire import Session
from .compactions import build_compactions
from .coverage import build_coverage
from .dispatch import build_dispatches
from .estimator import ESTIMATOR_VERSION
from .items import Extraction, extract_items
from .loads import build_loads
from .records import (
    AgentRecords,
    WireRecord,
    attribute_agents,
    detect_tier,
    group_records,
)
from .schema import (
    BUCKETS,
    AuditDocument,
    ContentItem,
    ContextAgent,
    ItemRef,
    LLMCall,
    OutputPart,
    RequestInput,
    RequestOutput,
    Span,
)
from .skills import build_skills, merge_skill_identities
from .spans import (
    RequestSpan,
    anchor_runs,
    positional_spans,
    verify_against_anchors,
)

ADAPTER_VERSION = "1.0"

OUTPUT_PART_TYPES = {
    "assistant_text": "text",
    "thinking": "think",
    "tool_call": "tool_call",
}


def _alive_items(
    items: List[ContentItem], agent_id: str, first_seq: int
) -> List[ContentItem]:
    alive: List[ContentItem] = []
    for item in items:
        if item.agent_id != agent_id:
            continue
        if item.wire_seq >= first_seq:
            continue
        if item.gone_seq is not None and first_seq >= item.gone_seq:
            continue
        alive.append(item)
    return alive


def alive_items_for_call(
    items: List[ContentItem], call: LLMCall
) -> List[ContentItem]:
    """Ledger items live when the call's span opens (item_id order)."""
    return _alive_items(items, call.agent_id, call.span.first_seq)


def _anchor_tokens(usage: Optional[dict]) -> Optional[int]:
    if not usage:
        return None
    value = usage.get("input_other")
    if not value:
        return None
    return int(value)


def _call_identity(span: RequestSpan, usage: Optional[dict]) -> Dict[str, Optional[str]]:
    request_id: Optional[str] = None
    request_hash: Optional[str] = None
    response_hash: Optional[str] = None
    for record in span.records:
        if request_id is None and record.request_id is not None:
            request_id = record.request_id
        if request_hash is None and record.request_hash is not None:
            request_hash = record.request_hash
            response_hash = record.response_hash
    if request_id is None and usage:
        request_id = usage.get("request_id")
    return {
        "request_id": request_id,
        "request_hash": request_hash,
        "response_hash": response_hash,
    }


def _identity_tier(usage_record: Optional[WireRecord]) -> int:
    if usage_record is None:
        return 2
    ref = usage_record.ref or {}
    if ref.get("request_hash") is not None and ref.get("request_id") is not None:
        return 1
    return 2


def _credits(usage: Optional[dict]) -> Optional[dict]:
    if not usage:
        return None
    keys = ("credits", "original_credits", "billable")
    if all(usage.get(key) is None for key in keys):
        return None
    return {key: usage.get(key) for key in keys}


def _build_call(
    agent_id: str, index: int, span: RequestSpan, items: List[ContentItem]
) -> LLMCall:
    usage_record = span.usage_record
    usage = usage_record.usage if usage_record is not None else None
    alive = _alive_items(items, agent_id, span.first_seq)
    buckets = {bucket: 0 for bucket in BUCKETS}
    for item in alive:
        if item.bucket in buckets:
            buckets[item.bucket] += item.tokens_est
    anchor = _anchor_tokens(usage)
    unattributed = None if anchor is None else anchor - sum(buckets.values())
    identity = _call_identity(span, usage)
    parts = [
        OutputPart(type=OUTPUT_PART_TYPES[item.kind], item_id=item.item_id)
        for item in items
        if item.agent_id == agent_id
        and item.kind in OUTPUT_PART_TYPES
        and span.first_seq <= item.wire_seq <= span.last_seq
    ]
    return LLMCall(
        call_id="%s:%d" % (agent_id, index),
        agent_id=agent_id,
        ts=span.records[0].ts,
        model=(usage or {}).get("model") or "",
        span=Span(span.first_seq, span.last_seq, span.n_records),
        identity_tier=_identity_tier(usage_record),
        input=RequestInput(
            item_refs=[
                ItemRef(ref.item_id, ref.bucket, ref.tokens_est) for ref in alive
            ],
            buckets=buckets,
            anchor_tokens=anchor,
            unattributed_tokens=unattributed,
            cache_read=int((usage or {}).get("input_cache_read") or 0),
            cache_write_5m=int((usage or {}).get("input_cache_creation_5m") or 0),
            cache_write_1h=int((usage or {}).get("input_cache_creation_1h") or 0),
            context_usage_ratio=(usage or {}).get("context_usage_ratio"),
            request_id=identity["request_id"],
            request_hash=identity["request_hash"],
            response_hash=identity["response_hash"],
            credits=_credits(usage),
            prefix_hashes=None,
        ),
        output=RequestOutput(
            parts=parts,
            output_tokens=int((usage or {}).get("output") or 0),
        ),
    )


def _runtime_facts(
    agent: AgentRecords,
) -> Tuple[Optional[str], Optional[int], Optional[dict]]:
    model: Optional[str] = None
    context_window: Optional[int] = None
    active_leaf: Optional[dict] = None
    for record in agent.records:
        for event in record.events:
            if event.type == "RuntimeConfig":
                payload = event.payload
                if payload.get("model") is not None:
                    model = payload.get("model")
                if payload.get("context_window") is not None:
                    context_window = payload.get("context_window")
            elif event.type == "ActiveLeaf":
                payload = event.payload
                active_leaf = {
                    "leaf_uuid": payload.get("leaf_uuid"),
                    "explicit": payload.get("explicit"),
                }
    return model, context_window, active_leaf


def _context_agent(agent: AgentRecords, calls: List[LLMCall]) -> ContextAgent:
    model, context_window, active_leaf = _runtime_facts(agent)
    return ContextAgent(
        agent_id=agent.agent_id,
        parent_id=agent.parent_id,
        origin_channel=agent.origin_channel,
        model=model,
        context_window=context_window,
        request_ids=[
            call.call_id for call in calls if call.agent_id == agent.agent_id
        ],
        active_leaf=active_leaf,
    )


def _dropped(events) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for event in events:
        if event.type == "RecordDropped":
            reason = (event.payload or {}).get("reason") or "unknown"
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _notes(adapter_id: str, tiers: Dict[str, int]) -> List[str]:
    notes: List[str] = []
    if adapter_id == "qoder":
        # These claims are verified for Qoder transcripts only: other
        # adapters' channels are not yet documented, and unverified claims
        # must not be stamped into coverage notes.
        notes.extend(
            [
                "%s: no system-prompt channel; the system bucket stays 0"
                % adapter_id,
                "%s: prefix_hashes are not exposed by the transcript; left null"
                % adapter_id,
            ]
        )
    for agent_id in sorted(tiers):
        if tiers[agent_id] == 3:
            notes.append(
                "%s: tier 3 (no request identity); records are ungroupable, "
                "no LLMCall built" % agent_id
            )
    return notes


def build_audit_document(session: Session, adapter_id: str) -> AuditDocument:
    records = group_records(session.events)
    agents = attribute_agents(records)

    extraction = Extraction()
    seq_ts: Dict[Tuple[str, int], Optional[float]] = {}
    tiers: Dict[str, int] = {}
    for agent in agents:
        agent_extraction = extract_items(agent.records, agent.agent_id)
        extraction.items.extend(agent_extraction.items)
        extraction.interventions.extend(agent_extraction.interventions)
        extraction.listing_lines.update(agent_extraction.listing_lines)
        extraction.envelope += agent_extraction.envelope
        extraction.signature += agent_extraction.signature
        extraction.conflicts += agent_extraction.conflicts
        extraction.human_text_dropped += agent_extraction.human_text_dropped
        for record in agent.records:
            seq_ts[(record.source, record.seq)] = record.ts
        tiers[agent.agent_id] = detect_tier(agent.records)

    # Dispatch join + brief classification (D3) runs before compactions and
    # span/bucket aggregation: the brief re-bucket (user -> inject) must be
    # in place when _build_call aggregates bucket tallies, so the invariant
    # "sum of buckets + unattributed == anchor" is computed once, from the
    # final classification (spec: the binding constraint wins over the
    # module-placement listing order).
    dispatches, phases, dispatch_links, phase_recognition = build_dispatches(
        agents, extraction.items, session.subagent_meta, seq_ts
    )

    compactions = build_compactions(agents, extraction.items)

    all_calls: List[LLMCall] = []
    checked_calls = 0
    disagreements: List[dict] = []
    ungroupable_records: List[WireRecord] = []
    for agent in agents:
        spans, ungroupable = positional_spans(agent.records)
        ungroupable_records.extend(ungroupable)
        if tiers[agent.agent_id] == 3:
            for span in spans:
                ungroupable_records.extend(span.records)
            continue
        if tiers[agent.agent_id] == 1:
            checked, found = verify_against_anchors(
                spans, anchor_runs(agent.records)
            )
            checked_calls += checked
            disagreements.extend(found)
        for index, span in enumerate(spans):
            all_calls.append(
                _build_call(agent.agent_id, index, span, extraction.items)
            )

    skills = build_skills(
        extraction.items, all_calls, seq_ts, extraction.listing_lines
    )
    listing_skill_ids = {
        skill_id
        for lines in extraction.listing_lines.values()
        for skill_id, _display, _tokens in lines
    }
    skills, skill_identity = merge_skill_identities(
        skills, listing_skill_ids
    )
    skill_loads, redundant_bodies = build_loads(
        skills, extraction.items, agents
    )
    channel_counts: Dict[str, int] = {}
    for load in skill_loads:
        if load.channel is not None:
            channel_counts[load.channel] = channel_counts.get(load.channel, 0) + 1
    skill_load_evidence = {
        "loads": sum(1 for load in skill_loads if load.kind == "load"),
        "reloads": sum(1 for load in skill_loads if load.kind == "reload"),
        "with_body": sum(
            1 for load in skill_loads if load.cost_basis == "body"
        ),
        "unavailable": sum(
            1 for load in skill_loads if load.cost_basis == "unavailable"
        ),
        "redundant_bodies": redundant_bodies,
        "channels": {
            channel: channel_counts[channel] for channel in sorted(channel_counts)
        },
    }
    agents_out = [_context_agent(agent, all_calls) for agent in agents]
    coverage = build_coverage(
        all_calls,
        extraction.items,
        tiers=tiers,
        checked_calls=checked_calls,
        disagreements=disagreements,
        ungroupable_records=ungroupable_records,
        extraction=extraction,
        dropped_records=_dropped(session.events),
        notes=_notes(adapter_id, tiers),
        skill_load_evidence=skill_load_evidence,
        dispatch_links=dispatch_links,
        phase_recognition=phase_recognition,
        skill_identity=skill_identity,
    )
    return AuditDocument(
        adapter={"id": adapter_id, "version": ADAPTER_VERSION},
        estimator_version=ESTIMATOR_VERSION,
        source_files=sorted({record.source for record in records}),
        agents=agents_out,
        requests=all_calls,
        items=extraction.items,
        skills=skills,
        compactions=compactions,
        coverage=coverage,
        skill_loads=skill_loads,
        dispatches=dispatches,
        phases=phases,
        interventions=extraction.interventions,
    )
