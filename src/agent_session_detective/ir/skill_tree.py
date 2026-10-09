# src/agent_session_detective/ir/skill_tree.py
"""The actual skill tree: agents as nodes, dispatch rows as edges.

Built from the document alone (loads, dispatches, skills, agents) — the
"what actually ran" counterpart to a playbook's static skill graph. A
node's ``attachments`` are the skills with at least one non-listing
observation (body/stub/execution) in that agent; ``ambient`` rolls up
the listings every agent silently re-injects. Edge ``to_agent_id`` is
null for orphan dispatches (no matching subagent transcript); orphan
edges are kept in ``edges`` so the renderer can show the gap, while
``totals["edges"]`` counts joined edges only. ``loose`` lists subagents
with no incoming dispatch edge — only when ``main`` exists to anchor
the tree.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .schema import AuditDocument, LoadEvidence, Observation, SkillEntity

ATTACHMENT_KINDS = ("body", "stub", "execution")

_HASH_RE = re.compile(r"([0-9a-f]{8,})$")


def _label(agent_id: str, subagent_types: Dict[str, Optional[str]]) -> str:
    subagent_type = subagent_types.get(agent_id)
    if subagent_type is None:
        return agent_id
    match = _HASH_RE.search(agent_id)
    if match is None:
        return subagent_type
    return "%s · %s" % (subagent_type, match.group(1)[:8])


def _attachment(skill: SkillEntity, observations: List[Observation],
                rows: List[LoadEvidence]) -> dict:
    body_sha1s = sorted({
        observation.body_sha1 for observation in observations
        if observation.body_sha1 is not None
    })
    channels = sorted({
        row.channel for row in rows if row.channel is not None
    })
    return {
        "skill_id": skill.skill_id,
        "name": skill.name,
        "aliases": list(skill.aliases),
        "executed": any(
            observation.kind == "execution" for observation in observations),
        "loads": [
            {
                "load_id": row.load_id,
                "kind": row.kind,
                "channel": row.channel,
                "ts": row.ts,
                "body_sha1": row.body_sha1,
                "cost_tokens_est": row.cost_tokens_est,
                "cost_basis": row.cost_basis,
            }
            for row in rows
        ],
        "loads_tokens_est": sum(
            row.cost_tokens_est or 0 for row in rows
            if row.cost_basis == "body"),
        "channels": channels,
        "sha1s": body_sha1s,
    }


def skill_tree(document: AuditDocument) -> dict:
    loads_by_agent: Dict[str, List[LoadEvidence]] = {}
    for load in document.skill_loads:
        loads_by_agent.setdefault(load.agent_id, []).append(load)

    subagent_types: Dict[str, Optional[str]] = {}
    edges: List[dict] = []
    for dispatch in document.dispatches:
        if dispatch.subagent_agent_id is not None:
            subagent_types.setdefault(
                dispatch.subagent_agent_id, dispatch.subagent_type)
        edges.append({
            "dispatch_id": dispatch.dispatch_id,
            "from_agent_id": dispatch.parent_agent_id,
            "to_agent_id": dispatch.subagent_agent_id,
            "subagent_type": dispatch.subagent_type,
            "description": dispatch.description,
            "brief_tokens_est": dispatch.brief_tokens_est,
            "phase_id": dispatch.phase_id,
        })

    parent_of: Dict[str, str] = {}
    via_dispatch: Dict[str, str] = {}
    for dispatch in document.dispatches:
        target = dispatch.subagent_agent_id
        if target is None or target in parent_of:
            continue
        parent_of[target] = dispatch.parent_agent_id
        via_dispatch[target] = dispatch.dispatch_id

    skills_by_id = {skill.skill_id: skill for skill in document.skills}
    nodes = []
    for agent in document.agents:
        attached: Dict[str, List[Observation]] = {}
        listing: Dict[str, List[int]] = {}
        for skill in document.skills:
            for observation in skill.observations:
                if observation.agent_id != agent.agent_id:
                    continue
                if observation.kind == "listing":
                    listing.setdefault(skill.skill_id, []).append(
                        observation.tokens_est)
                elif observation.kind in ATTACHMENT_KINDS:
                    attached.setdefault(skill.skill_id, []).append(observation)
        attachments = []
        for skill_id in sorted(attached):
            observations = attached[skill_id]
            rows = [load for load in loads_by_agent.get(agent.agent_id, [])
                    if load.skill_id == skill_id]
            attachments.append(
                _attachment(skills_by_id[skill_id], observations, rows))
        nodes.append({
            "agent_id": agent.agent_id,
            "label": _label(agent.agent_id, subagent_types),
            "model": agent.model,
            "context_window": agent.context_window,
            "n_requests": len(agent.request_ids),
            "parent_agent_id": parent_of.get(agent.agent_id),
            "via_dispatch_id": via_dispatch.get(agent.agent_id),
            "attachments": attachments,
            "ambient": {
                "skills": len(listing),
                "injections": sum(len(tokens) for tokens in listing.values()),
                "tokens_est": sum(
                    sum(tokens) for tokens in listing.values()),
            },
        })

    agent_ids = {agent.agent_id for agent in document.agents}
    loose = []
    if "main" in agent_ids:
        for agent in document.agents:
            if agent.agent_id == "main" or agent.agent_id in parent_of:
                continue
            loose.append({
                "agent_id": agent.agent_id,
                "label": _label(agent.agent_id, subagent_types),
                "reason": "no incoming dispatch edge",
            })

    totals = {
        "agents": len(document.agents),
        "edges": sum(1 for edge in edges if edge["to_agent_id"] is not None),
        "loads": sum(1 for load in document.skill_loads if load.kind == "load"),
        "reloads": sum(
            1 for load in document.skill_loads if load.kind == "reload"),
        "unavailable": sum(1 for load in document.skill_loads
                           if load.cost_basis == "unavailable"),
        "executions": sum(
            1 for skill in document.skills
            for observation in skill.observations
            if observation.kind == "execution"),
        "ambient_tokens_est": sum(
            node["ambient"]["tokens_est"] for node in nodes),
        "attached_tokens_est": sum(
            load.cost_tokens_est or 0
            for load in document.skill_loads if load.cost_basis == "body"),
    }
    return {"nodes": nodes, "edges": edges, "loose": loose, "totals": totals}
