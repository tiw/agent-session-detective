"""Skill entities: observations of skill listings, bodies, stubs, executions."""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Set, Tuple

from .schema import ContentItem, LLMCall, Observation, SkillEntity

BODY_CHANNELS = {
    "qoder:attachment:invoked_skills": "invoked_skills",
    "qoder:attachment:hook_output": "hook_output",
    "qoder:signature:skill_body": "user_signature",
}


def _call_for_seq(calls: List[LLMCall], agent_id: str, seq: int) -> Optional[str]:
    for call in calls:
        if call.agent_id == agent_id and call.span.first_seq <= seq <= call.span.last_seq:
            return call.call_id
    next_call: Optional[LLMCall] = None
    for call in calls:
        if call.agent_id != agent_id or call.span.first_seq <= seq:
            continue
        if next_call is None or call.span.first_seq < next_call.span.first_seq:
            next_call = call
    return next_call.call_id if next_call is not None else None


def _observation(item, kind, channel, tokens_est, call_id, ts, body_sha1=None):
    return Observation(
        kind=kind,
        channel=channel,
        agent_id=item.agent_id,
        call_id=call_id,
        ts=ts,
        tokens_est=tokens_est,
        body_sha1=body_sha1,
        item_id=item.item_id,
    )


def build_skills(
    items: List[ContentItem],
    calls: List[LLMCall],
    seq_ts: Dict[Tuple[str, int], Optional[float]],
    listing_lines: Dict[str, List[Tuple[str, str, int]]],
) -> List[SkillEntity]:
    names: Dict[str, str] = {}
    collected: Dict[str, List[Observation]] = {}

    for item in items:
        ts = seq_ts.get((item.record["file"], item.wire_seq))
        lines = listing_lines.get(item.item_id)
        if lines:
            call_id = _call_for_seq(calls, item.agent_id, item.wire_seq)
            for skill_id, display, tokens in lines:
                names.setdefault(skill_id, display)
                collected.setdefault(skill_id, []).append(
                    _observation(item, "listing", "skill_listing", tokens, call_id, ts))
            continue
        if not item.skill_id:
            continue
        # tool_call items carry the tool name ("Skill"), not the skill display name
        if item.name and item.kind != "tool_call":
            names.setdefault(item.skill_id, item.name)
        call_id = _call_for_seq(calls, item.agent_id, item.wire_seq)
        if item.kind == "skill_body":
            collected.setdefault(item.skill_id, []).append(_observation(
                item, "body", BODY_CHANNELS.get(item.channel, item.channel),
                item.tokens_est, call_id, ts, body_sha1=item.sha1))
        elif item.kind == "tool_result":
            collected.setdefault(item.skill_id, []).append(_observation(
                item, "body", "tool:read", item.tokens_est, call_id, ts,
                body_sha1=item.sha1))
        elif item.kind == "skill_stub":
            collected.setdefault(item.skill_id, []).append(_observation(
                item, "stub", "tool_result", item.tokens_est, call_id, ts))
        elif item.kind == "tool_call":
            collected.setdefault(item.skill_id, []).append(_observation(
                item, "execution", "skill_tool_call", item.tokens_est, call_id, ts))

    entities = []
    for skill_id in sorted(collected):
        observations = collected[skill_id]
        observations.sort(key=lambda o: (o.ts is None, o.ts or 0.0, o.item_id, o.kind))
        entities.append(SkillEntity(skill_id=skill_id,
                                    name=names.get(skill_id, skill_id),
                                    observations=observations))
    return entities


def _has_own_listing(entity: SkillEntity, listing_skill_ids: Set[str]) -> bool:
    if entity.skill_id in listing_skill_ids:
        return True
    return any(observation.kind == "listing"
               for observation in entity.observations)


def _merged_name(target: SkillEntity, absorbed: SkillEntity) -> str:
    if target.name != target.skill_id:
        return target.name
    if absorbed.name != absorbed.skill_id:
        return absorbed.name
    return absorbed.skill_id


def merge_skill_identities(
    entities: List[SkillEntity],
    listing_skill_ids: Set[str],
) -> Tuple[List[SkillEntity], dict]:
    """D1: one bare id merges into its unique ``:``-suffixed twin.

    A bare id stays unmerged when it has a listing of its own (it is a
    first-class skill) or when more than one other entity id ends with
    ``":" + bare`` (ambiguous — counted, never guessed). Absorbed
    observations keep ``raw_skill_id`` as provenance; the inputs are never
    mutated and item ids are never re-keyed.
    """
    absorbed_by_target: Dict[str, List[SkillEntity]] = {}
    absorbed_ids: Set[str] = set()
    ambiguous: List[str] = []
    for entity in entities:
        bare = entity.skill_id
        if ":" in bare or _has_own_listing(entity, listing_skill_ids):
            continue
        candidates = [other for other in entities
                      if other.skill_id.endswith(":" + bare)]
        if len(candidates) > 1:
            ambiguous.append(bare)
            continue
        if candidates:
            absorbed_by_target.setdefault(
                candidates[0].skill_id, []).append(entity)
            absorbed_ids.add(bare)
    merged_entities: List[SkillEntity] = []
    aliases: Dict[str, List[str]] = {}
    for entity in entities:
        if entity.skill_id in absorbed_ids:
            continue
        absorbed = absorbed_by_target.get(entity.skill_id)
        if not absorbed:
            merged_entities.append(entity)
            continue
        observations = list(entity.observations)
        for source in absorbed:
            observations.extend(
                replace(observation, raw_skill_id=source.skill_id)
                for observation in source.observations)
        observations.sort(key=lambda observation: (
            observation.ts is None, observation.ts or 0.0,
            observation.item_id, observation.kind))
        name = entity.name
        for source in absorbed:
            name = _merged_name(entity, source)
        merged_aliases = sorted(
            list(entity.aliases) + [source.skill_id for source in absorbed])
        aliases[entity.skill_id] = merged_aliases
        merged_entities.append(replace(
            entity, name=name, observations=observations,
            aliases=merged_aliases))
    merges = sum(len(a) for a in absorbed_by_target.values())
    report = {
        "merges": merges,
        "aliases": aliases,
        "ambiguous": sorted(ambiguous),
        "note": ("bare ids merge into a unique namespaced twin unless they "
                 "have a listing of their own; listed or ambiguous ids stay "
                 "unmerged"),
    }
    return merged_entities, report
