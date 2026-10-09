# src/agent_session_detective/ir/analyses.py
"""The spec analyses, computed from the AuditDocument alone.

- ``skill_audit``: listing re-injections and version drift, body channel
  distribution, stub-vs-body gap per SkillEntity.
- ``context_organization``: per-call bucket matrix, unattributed share,
  new-vs-re-injected tokens for inject+skill items (spec rule: a sha1 seen
  in an earlier request of the same agent is a re-injection), compaction
  impact (killed and restored items).
- ``redundancy``: cross-bucket ``norm_sha1`` groups, exact re-sends via
  ``request_hash`` equality (tier 1 only — tier 2 has no request hash), and
  uselessness heuristics that are labeled ``inferred``.
- ``skill_loads``: the IR 1.1 load-evidence ledger rows plus rollups
  (loads / reloads / unavailable, summed EST cost, channel distribution;
  ``redundant_bodies`` read from the coverage block, which counts rows
  that by design have no ledger row).
- ``dispatches``: the IR 1.1 dispatch join rows, phase entities, and the
  coverage link/phase-recognition blocks.

The v1 FIFO simplification is retired (IR 1.1 carries ``tool_use_id``):
``tool_result_without_call`` / ``tool_call_without_result`` flags are
id-exact via ``ContentItem.tool_use_id`` — a result pairs with the call
carrying the same id (string equality); an item without an id cannot prove
a pair and is flagged.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Dict, List, Set, Tuple

from .schema import AuditDocument

RE_INJECTION_BUCKETS = ("inject", "skill")


def _item_position(item) -> Tuple[str, int, str]:
    return (item.agent_id, item.wire_seq, item.item_id)


def skill_audit(document: AuditDocument) -> List[dict]:
    items_by_id = {item.item_id: item for item in document.items}
    audits = []
    for skill in document.skills:
        listing = {"injections": 0, "tokens_est": 0, "repeat_tokens_est": 0,
                   "distinct_versions": 0, "versions": []}
        bodies = {"observations": 0, "tokens_est": 0, "channels": [],
                  "sha1s": [], "version_drift": False,
                  "without_execution": False}
        stubs = 0
        executions = 0
        seen_listing = set()
        versions: Dict[str, dict] = {}
        channels = set()
        body_sha1s = set()
        for observation in skill.observations:
            if observation.kind == "listing":
                listing["injections"] += 1
                listing["tokens_est"] += observation.tokens_est
                sha1 = items_by_id[observation.item_id].sha1
                if sha1 in seen_listing:
                    listing["repeat_tokens_est"] += observation.tokens_est
                else:
                    seen_listing.add(sha1)
                group = versions.get(sha1)
                if group is None:
                    versions[sha1] = {
                        "sha1": sha1,
                        "first_item_id": observation.item_id,
                        "injections": 1,
                        "tokens_est": observation.tokens_est,
                    }
                else:
                    group["injections"] += 1
                    group["tokens_est"] += observation.tokens_est
            elif observation.kind == "body":
                bodies["observations"] += 1
                bodies["tokens_est"] += observation.tokens_est
                channels.add(observation.channel)
                if observation.body_sha1 is not None:
                    body_sha1s.add(observation.body_sha1)
            elif observation.kind == "stub":
                stubs += 1
            elif observation.kind == "execution":
                executions += 1
        listing["versions"] = sorted(
            versions.values(),
            key=lambda group: _item_position(
                items_by_id[group["first_item_id"]]
            ),
        )
        listing["distinct_versions"] = len(listing["versions"])
        bodies["channels"] = sorted(channels)
        bodies["sha1s"] = sorted(body_sha1s)
        bodies["version_drift"] = len(body_sha1s) > 1
        bodies["without_execution"] = (
            bodies["observations"] > 0 and executions == 0
        )
        audits.append({
            "skill_id": skill.skill_id,
            "name": skill.name,
            "listing": listing,
            "bodies": bodies,
            "stubs": stubs,
            "executions": executions,
            "stub_body_ratio": None if stubs + bodies["observations"] == 0
            else round(stubs / (stubs + bodies["observations"]), 4),
        })
    return audits


def context_organization(document: AuditDocument) -> dict:
    items_by_id = {item.item_id: item for item in document.items}
    seen_by_agent: Dict[str, set] = {}
    rows = []
    for call in sorted(document.requests,
                       key=lambda call: (call.agent_id, call.span.first_seq)):
        seen = seen_by_agent.setdefault(call.agent_id, set())
        new_tokens = 0
        re_tokens = 0
        for ref in call.input.item_refs:
            if ref.bucket not in RE_INJECTION_BUCKETS:
                continue
            sha1 = items_by_id[ref.item_id].sha1
            if sha1 in seen:
                re_tokens += ref.tokens_est
            else:
                seen.add(sha1)
                new_tokens += ref.tokens_est
        anchor = call.input.anchor_tokens
        unattributed = call.input.unattributed_tokens
        rows.append({
            "call_id": call.call_id,
            "agent_id": call.agent_id,
            "n_items": len(call.input.item_refs),
            "anchor_tokens": anchor,
            "unattributed_tokens": unattributed,
            "unattributed_share": None if not anchor or unattributed is None
            else round(unattributed / anchor, 4),
            "buckets": dict(call.input.buckets),
            "new_tokens_est": new_tokens,
            "re_injected_tokens_est": re_tokens,
        })
    compactions = [
        {
            "compaction_id": compaction.compaction_id,
            "agent_id": compaction.agent_id,
            "boundary_seq": compaction.boundary_seq,
            "trigger": compaction.trigger,
            "pre_tokens": compaction.pre_tokens,
            "post_tokens": compaction.post_tokens,
            "killed_item_ids": [
                item.item_id
                for item in document.items
                if item.agent_id == compaction.agent_id
                and item.gone_seq == compaction.boundary_seq
            ],
            "restored_item_ids": list(compaction.restored_item_ids),
        }
        for compaction in document.compactions
    ]
    return {"rows": rows, "compactions": compactions}


def redundancy(document: AuditDocument) -> dict:
    items_by_id = {item.item_id: item for item in document.items}

    norm_groups: Dict[str, List] = {}
    for item in document.items:
        norm_groups.setdefault(item.norm_sha1, []).append(item)
    duplicate_groups = []
    for norm_sha1, members in norm_groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=_item_position)
        buckets = sorted({item.bucket for item in members})
        duplicate_groups.append({
            "norm_sha1": norm_sha1,
            "item_ids": [item.item_id for item in members],
            "buckets": buckets,
            "cross_bucket": len(buckets) > 1,
            "tokens_est": sum(item.tokens_est for item in members),
            "repeat_tokens_est": sum(item.tokens_est for item in members[1:]),
        })

    resend_groups: Dict[Tuple[str, str], List[str]] = {}
    for call in document.requests:
        request_hash = call.input.request_hash
        if request_hash is None:
            continue
        resend_groups.setdefault(
            (call.agent_id, request_hash), []
        ).append(call.call_id)
    exact_resend_groups = [
        {"agent_id": agent_id, "request_hash": request_hash,
         "call_ids": call_ids}
        for (agent_id, request_hash), call_ids in resend_groups.items()
        if len(call_ids) >= 2
    ]

    flags: Dict[str, str] = {}
    call_ids: Set[Tuple[str, str]] = {
        (item.agent_id, item.tool_use_id)
        for item in document.items
        if item.kind == "tool_call" and item.tool_use_id is not None
    }
    result_ids: Set[Tuple[str, str]] = {
        (item.agent_id, item.tool_use_id)
        for item in document.items
        if item.kind in ("tool_result", "skill_stub")
        and item.tool_use_id is not None
    }
    for item in document.items:
        if item.kind == "tool_call":
            if item.tool_use_id is None or (
                item.agent_id, item.tool_use_id
            ) not in result_ids:
                flags.setdefault(item.item_id, "tool_call_without_result")
        elif item.kind in ("tool_result", "skill_stub"):
            if item.tool_use_id is None or (
                item.agent_id, item.tool_use_id
            ) not in call_ids:
                flags.setdefault(item.item_id, "tool_result_without_call")

    for skill in document.skills:
        if any(o.kind == "execution" for o in skill.observations):
            continue
        for observation in skill.observations:
            if observation.kind == "body":
                flags.setdefault(
                    observation.item_id, "skill_body_without_execution"
                )

    reference_counts: Dict[str, int] = {}
    for call in document.requests:
        for ref in call.input.item_refs:
            reference_counts[ref.item_id] = (
                reference_counts.get(ref.item_id, 0) + 1
            )
    for item in document.items:
        if item.gone_seq is not None and reference_counts.get(
                item.item_id, 0) == 1:
            flags.setdefault(item.item_id, "short_lived_before_compaction")

    useless = [
        {"item_id": item_id, "reason": reason, "inferred": True}
        for item_id, reason in sorted(
            flags.items(),
            key=lambda pair: _item_position(items_by_id[pair[0]]),
        )
    ]
    return {"duplicate_groups": duplicate_groups,
            "exact_resend_groups": exact_resend_groups,
            "useless": useless}


def skill_loads(document: AuditDocument) -> dict:
    """Load-ledger rows plus rollups. Cost sums cover non-null rows only and
    stay labeled EST; ``redundant_bodies`` comes from the coverage block
    (redundant bodies are counted without rows, per the join rule)."""
    rows = [asdict(load) for load in document.skill_loads]
    channels: Dict[str, int] = {}
    cost_tokens_est = 0
    for row in rows:
        if row["cost_basis"] != "body":
            continue
        cost_tokens_est += row["cost_tokens_est"] or 0
        if row["channel"] is not None:
            channels[row["channel"]] = channels.get(row["channel"], 0) + 1
    coverage_block = document.coverage.skill_load_evidence or {}
    return {
        "rows": rows,
        "totals": {
            "loads": sum(1 for row in rows if row["kind"] == "load"),
            "reloads": sum(1 for row in rows if row["kind"] == "reload"),
            "unavailable": sum(
                1 for row in rows if row["cost_basis"] == "unavailable"
            ),
            "redundant_bodies": coverage_block.get("redundant_bodies", 0),
            "cost_tokens_est": cost_tokens_est,  # EST: summed body tokens_est
        },
        "channels": {channel: channels[channel] for channel in sorted(channels)},
    }


def dispatches(document: AuditDocument) -> dict:
    """Dispatch join rows, phase entities, and the coverage blocks."""
    return {
        "rows": [asdict(dispatch) for dispatch in document.dispatches],
        "phases": [
            {
                "phase_id": phase.phase_id,
                "name": phase.name,
                "observations": [asdict(obs) for obs in phase.observations],
            }
            for phase in document.phases
        ],
        "links": dict(document.coverage.dispatch_links or {}),
        "phase_recognition": dict(document.coverage.phase_recognition or {}),
    }


def build_analyses(document: AuditDocument) -> dict:
    """The spec analyses keyed for report/CLI consumption."""
    return {
        "skill_audit": skill_audit(document),
        "context_organization": context_organization(document),
        "redundancy": redundancy(document),
        "skill_loads": skill_loads(document),
        "dispatches": dispatches(document),
    }
