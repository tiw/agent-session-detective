# src/agent_session_detective/ir/gap.py
"""Skill GAP analysis: catalog expectations × IR actual observations.

Three sources meet here, each keeping its own epistemic status:

- IR state (``SkillEntity`` + ``ContentItem``): what the session actually
  did — listings, bodies, stubs, executions per skill id.
- ``catalog`` (disk SKILL.md): what each skill declares — the trigger
  wording an agent sees when deciding to load it.
- ``judgments``: counterfactual trigger verdicts about never-loaded
  candidates. LLM output, carried with verbatim evidence or discarded.

The join key is the lowercased skill name across IR skill ids (plus
identity-merge aliases) and catalog names. Join failures are findings,
not guesses: they are listed as ``ir_only``, never renamed to match.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..catalog import Skill
from .schema import ContentItem, SkillEntity


def _counts(skills: Sequence[SkillEntity]) -> Dict[str, dict]:
    """Per skill id: listed / bodies / stubs / executions and listing cost."""
    out: Dict[str, dict] = {}
    for skill in skills:
        entry = out.setdefault(skill.skill_id, {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "aliases": list(skill.aliases),
            "listed": 0,
            "bodies": 0,
            "stubs": 0,
            "executed": 0,
            "listing_tokens_est": 0,
        })
        for obs in skill.observations:
            if obs.kind == "listing":
                entry["listed"] += 1
                entry["listing_tokens_est"] += obs.tokens_est
            elif obs.kind == "body":
                entry["bodies"] += 1
            elif obs.kind == "stub":
                entry["stubs"] += 1
            elif obs.kind == "execution":
                entry["executed"] += 1
        entry["loaded"] = entry["bodies"] + entry["stubs"]
    return out


def _join(counts: Dict[str, dict], catalog: Sequence[Skill]):
    """Match IR ids (+aliases) against catalog names, lowercased.

    Returns ``(matched: skill_id -> Skill, ir_only rows, catalog_only count)``.
    """
    by_name: Dict[str, Skill] = {}
    for skill in catalog:
        by_name.setdefault(skill.name.lower(), skill)
    matched: Dict[str, Skill] = {}
    ir_only: List[dict] = []
    for skill_id, entry in counts.items():
        keys = [skill_id.lower()] + [a.lower() for a in entry["aliases"]]
        hit = next((by_name[k] for k in keys if k in by_name), None)
        if hit is None:
            ir_only.append({
                "skill_id": skill_id,
                "name": entry["name"],
                "listed": entry["listed"],
                "loaded": entry["loaded"],
                "executed": entry["executed"],
            })
        else:
            matched[skill_id] = hit
    matched_catalog_names = {s.name.lower() for s in matched.values()}
    catalog_only = len(by_name) - len(matched_catalog_names)
    return matched, ir_only, catalog_only


def _describe(counts: Dict[str, dict], matched: Dict[str, Skill],
              skill_id: str) -> dict:
    entry = counts[skill_id]
    catalog_skill = matched.get(skill_id)
    return {
        "skill_id": skill_id,
        "name": entry["name"],
        "description": catalog_skill.description if catalog_skill else None,
        "bodies": entry["bodies"],
        "stubs": entry["stubs"],
    }


def skill_gaps(skills: Sequence[SkillEntity], catalog: Sequence[Skill],
               judgments, stats: Optional[dict] = None,
               judge_configured: bool = True) -> dict:
    """The GAP block: join health, candidate pool, verdicts, leftovers.

    ``stats`` is the dict returned by ``gap_candidates`` for the same
    inputs; when absent, unjudged accounting degrades to counts only.
    """
    counts = _counts(skills)
    matched, ir_only, catalog_only = _join(counts, catalog)
    listed_unloaded = sorted(
        skill_id for skill_id, e in counts.items()
        if e["listed"] and not e["loaded"] and not e["executed"])
    loaded_unused = sorted(
        skill_id for skill_id, e in counts.items()
        if e["bodies"] and not e["executed"])

    id_by_catalog_name: Dict[str, str] = {}
    for skill_id, catalog_skill in matched.items():
        id_by_catalog_name[catalog_skill.name.lower()] = skill_id

    missed = []
    judge_errors = 0
    for j in judgments:
        if j.error:
            judge_errors += 1
        elif j.triggered:
            missed.append({
                "skill_id": id_by_catalog_name.get(j.skill_name.lower()),
                "name": j.skill_name,
                "turn": j.turn,
                "rationale": j.rationale,
                "evidence": j.evidence,
                "confidence": j.confidence,
            })

    candidates = stats["candidates"] if stats else 0
    # judged_ok excludes defects: an error row means this candidate has no
    # valid verdict, so it counts as unjudged. When the judge never ran,
    # judged_ok is 0 and every candidate lands here — which is the truth.
    judged_ok = sum(1 for j in judgments if not j.error)
    over_limit = (candidates - judged_ok) if stats else 0
    prefiltered = stats["prefiltered"] if stats else 0
    if stats and not judge_configured:
        unjudged_count = stats["listed_unloaded"]
    else:
        unjudged_count = prefiltered + over_limit
    unjudged = {
        "count": unjudged_count,
        "prefiltered": prefiltered,
        "over_limit": over_limit,
        "judge_errors": judge_errors,
        "judge_unconfigured": not judge_configured,
    }

    notes = []
    if not judge_configured:
        notes.append("judge not configured — trigger GAPs not evaluated")
    if ir_only:
        notes.append("%d IR skill(s) matched no catalog entry" % len(ir_only))
    if prefiltered:
        notes.append(
            "%d listed-unloaded skill(s) prefiltered (name absent from "
            "session input, or no catalog wording to judge against)"
            % prefiltered)

    return {
        "join": {"matched": len(matched), "ir_only": ir_only,
                 "catalog_only": catalog_only},
        "sets": {
            "listed": sum(1 for e in counts.values() if e["listed"]),
            "loaded": sum(1 for e in counts.values() if e["loaded"]),
            "executed": sum(1 for e in counts.values() if e["executed"]),
        },
        "listed_unloaded": [
            _describe(counts, matched, s) for s in listed_unloaded],
        "missed_triggers": missed,
        "loaded_unused": [
            _describe(counts, matched, s) for s in loaded_unused],
        "unjudged": unjudged,
        "notes": notes,
    }


def gap_candidates(*args, **kwargs):
    """Placeholder — implemented in the next task."""
    raise NotImplementedError
