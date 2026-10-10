"""Coverage: re-derived provenance counts, tiers, bucket sources, drops.

``fact`` counts measured values: ``None`` means not reported, so a measured
zero still counts as fact; ``tokens_est`` is the one estimated entry.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .items import Extraction
from .schema import ContentItem, CoverageReport, Intervention, LLMCall

TRACKED_FIELDS = (
    "anchor_tokens",
    "request_id",
    "request_hash",
    "response_hash",
    "context_usage_ratio",
    "credits",
)
DERIVATION_RULE = "positional: user opens a turn; positive usage closes the span"


def _per_field(calls: List[LLMCall], items: List[ContentItem]) -> Dict[str, dict]:
    per_field = {}
    for name in TRACKED_FIELDS:
        fact = sum(1 for call in calls if getattr(call.input, name) is not None)
        per_field[name] = {"fact": fact, "est": 0, "missing": len(calls) - fact}
    per_field["tokens_est"] = {"fact": 0, "est": len(items), "missing": 0}
    return per_field


def _unknown_channels(items: List[ContentItem]) -> List[dict]:
    counted: Dict[str, dict] = {}
    for item in items:
        if item.kind != "unknown":
            continue
        entry = counted.setdefault(
            item.channel,
            {"channel": item.channel, "count": 0, "tokens_est": 0},
        )
        entry["count"] += 1
        entry["tokens_est"] += item.tokens_est
    return [counted[channel] for channel in sorted(counted)]


def _intervention_evidence(interventions: List[Intervention]) -> dict:
    """Distinct-lead intervention counts re-derived from the shipped rows.

    The unit is the carrier-stripped lead, not the row: the harness re-emits
    some leads on every turn, and counting rows would let machine repetition
    masquerade as operator friction. A lead that classifies to two labels is
    counted once, under its first-seen label, and flagged in
    ``label_conflicts`` — never silently merged.
    """
    label_by_lead: Dict[str, str] = {}
    for row in interventions:
        label_by_lead.setdefault(row.lead_sha1, row.label)
    conflicts = sorted(
        {row.lead_sha1 for row in interventions
         if label_by_lead[row.lead_sha1] != row.label}
    )
    counts: Dict[str, int] = {}
    for label in label_by_lead.values():
        counts[label] = counts.get(label, 0) + 1
    leads = len(label_by_lead)
    residue = counts.get("unclassified", 0)
    return {
        "rows": len(interventions),
        "leads": leads,
        "labels": {label: counts[label] for label in sorted(counts)},
        "residue": residue,
        "residue_rate": "%d/%d" % (residue, leads),
        "label_conflicts": conflicts,
        "note": ("counts are distinct carrier-stripped leads (lead_sha1), not "
                 "rows; residue_rate is the unclassified share of distinct "
                 "leads; a lead seen under two labels counts once, under its "
                 "first-seen label"),
    }


def _human_text_evidence(items: List[ContentItem], dropped_non_main: int) -> dict:
    """Native operator keystrokes: stamps that shipped vs drops at extraction.

    ``stamped`` is re-derived from the document's items. ``dropped_non_main``
    is the extraction-time counter: a withheld stamp is indistinguishable from
    a turn that never had one, by design — the text (the parent agent's Task
    brief) is what must not read as operator keystrokes.
    """
    stamped = sum(1 for item in items if item.human_text is not None)
    return {
        "stamped": stamped,
        "dropped_non_main": dropped_non_main,
        "note": ("stamped counts shipped items carrying the raw humanInput "
                 "text; dropped_non_main counts non-main TurnBegin records "
                 "whose humanInput text was withheld from the stamp (the "
                 "parent agent's Task brief must not read as operator "
                 "keystrokes)"),
    }


def build_coverage(
    calls: List[LLMCall],
    items: List[ContentItem],
    *,
    tiers: Dict[str, int],
    checked_calls: int,
    disagreements: List[dict],
    ungroupable_records: List,
    extraction: Extraction,
    dropped_records: Dict[str, int],
    notes: List[str],
    skill_load_evidence: Optional[dict] = None,
    dispatch_links: Optional[dict] = None,
    phase_recognition: Optional[dict] = None,
    skill_identity: Optional[dict] = None,
) -> CoverageReport:
    anchored = sum(1 for call in calls if call.input.anchor_tokens is not None)
    tier_counts = {1: 0, 2: 0, 3: 0}
    for tier in tiers.values():
        if tier in tier_counts:
            tier_counts[tier] += 1
    return CoverageReport(
        per_field=_per_field(calls, items),
        requests_with_anchor="%d/%d" % (anchored, len(calls)),
        request_identity={
            "tier1_sessions": tier_counts[1],
            "tier2_sessions": tier_counts[2],
            "tier3_ungroupable_sessions": tier_counts[3],
            "derived_verification": {
                "rule": DERIVATION_RULE,
                "checked_calls": checked_calls,
                "disagreements": len(disagreements),
            },
            "ungroupable_records": len(ungroupable_records),
        },
        bucket_sources={
            "envelope": extraction.envelope,
            "signature": extraction.signature,
            "envelope_vs_signature_conflicts": extraction.conflicts,
        },
        unknown_channels=_unknown_channels(items),
        dropped_records={
            reason: count for reason, count in sorted(dropped_records.items())
        },
        notes=list(notes),
        skill_load_evidence=skill_load_evidence or {},
        dispatch_links=dispatch_links or {},
        phase_recognition=phase_recognition or {},
        skill_identity=skill_identity or {},
        intervention_evidence=_intervention_evidence(extraction.interventions),
        human_text_evidence=_human_text_evidence(
            items, extraction.human_text_dropped
        ),
    )
