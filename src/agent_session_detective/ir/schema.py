# src/agent_session_detective/ir/schema.py
"""Audit IR dataclasses (design: docs/superpowers/specs/2026-10-08-audit-ir-design.md).

Pure data: no I/O, no parsing. ``to_dict`` is ``dataclasses.asdict`` so the
JSON shape follows the field order here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import __version__ as ASD_VERSION

IR_VERSION = "1.0"

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


@dataclass
class SkillEntity:
    skill_id: str
    name: str
    observations: List[Observation]


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
