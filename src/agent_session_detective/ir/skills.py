"""Skill entities: observations of skill listings, bodies, stubs, executions."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

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
