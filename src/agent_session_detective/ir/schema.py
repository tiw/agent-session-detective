# src/agent_session_detective/ir/schema.py
"""Audit IR dataclasses (design: docs/superpowers/specs/2026-10-08-audit-ir-design.md).

Pure data: no I/O, no parsing. ``to_dict`` is ``dataclasses.asdict`` so the
JSON shape follows the field order here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import __version__ as ASD_VERSION

IR_VERSION = "1.6"

BUCKETS: Tuple[str, ...] = (
    "system",
    "tools",
    "user",
    "inject",
    "skill",
    "assistant",
    "tool",
)
UNATTRIBUTED = "unattributed"


@dataclass
class ItemRef:
    item_id: str
    bucket: str
    tokens_est: int


@dataclass
class Span:
    first_seq: int
    last_seq: int
    n_records: int


@dataclass
class RequestInput:
    item_refs: List[ItemRef]
    buckets: Dict[str, int]
    anchor_tokens: Optional[int]
    unattributed_tokens: Optional[int]
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    context_usage_ratio: Optional[float]
    request_id: Optional[str]
    request_hash: Optional[str]
    response_hash: Optional[str]
    credits: Optional[dict]
    prefix_hashes: Optional[dict]  # {"system": str|None, "tools": str|None}; None for Qoder — never fabricated


@dataclass
class OutputPart:
    type: str
    item_id: Optional[str]


@dataclass
class RequestOutput:
    parts: List[OutputPart]
    output_tokens: int


@dataclass
class LLMCall:
    call_id: str
    agent_id: str
    ts: Optional[float]
    model: str
    span: Span
    identity_tier: int
    input: RequestInput
    output: RequestOutput


@dataclass
class ContentItem:
    item_id: str
    agent_id: str
    bucket: str
    kind: str
    name: Optional[str]
    channel: str
    wire_seq: int
    gone_seq: Optional[int]
    size_chars: int
    tokens_est: int
    sha1: str
    norm_sha1: str
    record: dict
    preview: str
    skill_id: Optional[str] = None
    # Tool identity (IR 1.1): Qoder ToolCall payload "id" / ToolResult
    # payload "tool_call_id". FACT — joins use string equality only.
    tool_use_id: Optional[str] = None
    # Phase stamp (IR 1.1): set by the builder when a dispatch's tier A/B
    # recognition assigns one phase; null means no attribution (never guessed).
    phase_id: Optional[str] = None
    # Typed text (IR 1.3): the log's own humanInput.text — not the harness
    # expansion that preview/size_chars measure and token attribution counts.
    human_text: Optional[str] = None


@dataclass
class Observation:
    kind: str
    channel: str
    agent_id: str
    call_id: Optional[str]
    ts: Optional[float]
    tokens_est: int
    body_sha1: Optional[str]
    item_id: str
    # Identity merge (IR 1.2): the id this observation was recorded under
    # before a bare id merged into its namespaced twin; null = canonical.
    raw_skill_id: Optional[str] = None


@dataclass
class SkillEntity:
    skill_id: str
    name: str
    observations: List[Observation]
    # Identity merge (IR 1.2): bare ids absorbed into this entity, sorted.
    aliases: List[str] = field(default_factory=list)


@dataclass
class Compaction:
    compaction_id: str
    agent_id: str
    ts: Optional[float]
    boundary_seq: int
    trigger: Optional[str]
    pre_tokens: Optional[int]
    post_tokens: Optional[int]
    messages_summarized: Optional[int]
    duration_ms: Optional[int]
    restored_item_ids: List[str]


@dataclass
class ContextAgent:
    agent_id: str
    parent_id: Optional[str]
    origin_channel: str
    model: Optional[str]
    context_window: Optional[int]
    request_ids: List[str]
    active_leaf: Optional[dict]  # {"leaf_uuid": str, "explicit": bool} or None (spec keys on the leaf record)


@dataclass
class CoverageReport:
    per_field: Dict[str, dict]
    requests_with_anchor: str
    request_identity: dict
    bucket_sources: dict
    unknown_channels: List[dict]
    dropped_records: Dict[str, int]
    notes: List[str]
    # IR 1.1 evidence blocks: counted, re-derived from the document, never
    # self-declared. Absent evidence is counted, not omitted.
    skill_load_evidence: dict = field(default_factory=dict)
    dispatch_links: dict = field(default_factory=dict)
    phase_recognition: dict = field(default_factory=dict)
    # IR 1.2: bare-id → namespaced-twin merges, ambiguities counted.
    skill_identity: dict = field(default_factory=dict)
    # IR 1.6: distinct-lead intervention counts, re-derived from the rows.
    intervention_evidence: dict = field(default_factory=dict)


@dataclass
class LoadEvidence:
    """One skill-load row (IR 1.1): a stub marker joined to its body item.

    ``kind`` is "load" for the first body attached to a stub, "reload" when a
    later body arrives after the previous body was compacted away. A cost is
    the attached body's ``tokens_est`` (EST) or ``unavailable`` with
    ``cost_tokens_est: null`` — a stub-derived number is never rendered.
    """

    load_id: str
    agent_id: str
    skill_id: str
    kind: str  # "load" | "reload"
    marker_item_id: Optional[str]
    body_item_id: Optional[str]
    body_sha1: Optional[str]
    channel: Optional[str]
    ts: Optional[float]
    cost_tokens_est: Optional[int]
    cost_basis: str  # "body" | "unavailable"


@dataclass
class Dispatch:
    """One Agent tool call joined (string-equality on tool_use_id) to a
    subagent transcript, with its brief and phase recognition (IR 1.1)."""

    dispatch_id: str
    parent_agent_id: str
    tool_use_id: str
    tool_item_id: str
    subagent_agent_id: Optional[str]  # null for orphan calls
    brief_item_id: Optional[str]
    brief_tokens_est: Optional[int]
    subagent_type: Optional[str]
    description: Optional[str]
    meta: Optional[dict]
    phase_refs: List[dict]
    phase_id: Optional[str]


@dataclass
class PhaseObservation:
    """One piece of evidence behind a phase (IR 1.1).

    ``kind: "dispatch"`` carries ``dispatch_id``; ``kind: "read"`` carries
    ``item_id``. A multi-ref brief's dispatch observation carries
    ``tokens_est: null`` so its cost is never counted across two phases.
    """

    kind: str  # "dispatch" | "read"
    agent_id: str
    dispatch_id: Optional[str] = None
    item_id: Optional[str] = None
    ts: Optional[float] = None
    tokens_est: Optional[int] = None
    channel: Optional[str] = None


@dataclass
class PhaseEntity:
    """A recognized phase (IR 1.1): exists only where recognition evidence
    exists — tier A (brief is the skill body) or tier B (rule match). A
    phase attribution without evidence is never fabricated."""

    phase_id: str
    name: Optional[str] = None
    observations: List[PhaseObservation] = field(default_factory=list)


@dataclass
class Intervention:
    """One operator-intervention verdict for a main-agent user message (IR 1.6).

    Exactly one row per main-agent ``user_message`` item, including
    ``unclassified`` residue rows. ``lead_sha1`` is sha1 of the
    carrier-stripped lead — the distinct-lead dedup key; ``rule`` names the
    cascade rule that fired (verbatim evidence, empty for residue).
    """

    item_id: str
    label: str
    rule: str
    evidence: str
    carriers: List[str]
    lead_sha1: str


@dataclass
class BilledUsage:
    """Provider-billed token totals (opt-in side-channel, attached post-build).

    Sourced from Qoder's local SharedClientCache DB. EST (per-skill
    attribution) and billed (session totals) are separate lenses: every
    number here is a provider-billed fact; when nothing parseable was
    observed the field stays null and a coverage note explains why.
    """

    session_id: str        # transcript uuid used for the SharedClientCache join
    source: str            # provenance, e.g. "SharedClientCache chat_message.token_info"
    db_path: str           # the DB file actually queried
    requests: int          # parseable token_info rows
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    rows_total: int        # all chat_message rows seen for the session
    rows_without_token_info: int    # rows skipped (no parseable token_info, e.g. user/tool turns)


@dataclass
class AuditDocument:
    adapter: Dict[str, str]
    estimator_version: str
    source_files: List[str]
    agents: List[ContextAgent]
    requests: List[LLMCall]
    items: List[ContentItem]
    skills: List[SkillEntity]
    compactions: List[Compaction]
    coverage: CoverageReport
    skill_loads: List[LoadEvidence] = field(default_factory=list)
    dispatches: List[Dispatch] = field(default_factory=list)
    phases: List[PhaseEntity] = field(default_factory=list)
    interventions: List[Intervention] = field(default_factory=list)
    # Opt-in side-channel: attached post-build, never produced by the
    # builder; null = not requested, or unavailable (see coverage.notes).
    billing: Optional[BilledUsage] = None
    ir_version: str = IR_VERSION
    generator: dict = field(
        default_factory=lambda: {
            "name": "agent-session-detective",
            "version": ASD_VERSION,
        }
    )

    def to_dict(self) -> dict:
        return asdict(self)


# TODO(T2): estimator, records, spans, items, skills, compactions, coverage,
# builder, analyses modules follow in later tasks.
