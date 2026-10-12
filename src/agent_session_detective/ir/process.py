"""Process signature: how much workflow machinery a session ran on.

直通 vs pipeline is an observational comparison, not a randomized trial.
The signature reports machinery features — harness injections, catalog
re-offers (listing re-injection), skill loads, workflow state-file
touches — plus a coarse style verdict built only on presence, never on
uncalibrated thresholds. The fleet layer groups sessions by style so
dependent variables (ceremony cost, compactions, interventions) can be
compared side by side; task difficulty is a confound the presentation
states, not one this module pretends to control.
"""

from __future__ import annotations

import re
from typing import Dict

from .schema import AuditDocument

# Workflow state: the paze-family flows keep their progress ledger in
# progress.md / the .changes tree, so a read or write of either marks the
# session as running on that machinery.
WORKFLOW_FILE_RE = re.compile(
    r"(^|[\\/])progress\.md$|[\\/]\.changes([\\/]|$)", re.IGNORECASE)
READ_TOOLS = {"read"}
WRITE_TOOLS = {"write", "edit"}


def _workflow(document: AuditDocument) -> Dict[str, int]:
    reads = 0
    writes = 0
    for item in document.items:
        if item.kind != "tool_call" or not item.file_path:
            continue
        if not WORKFLOW_FILE_RE.search(item.file_path):
            continue
        lowered = (item.name or "").lower()
        if lowered in READ_TOOLS:
            reads += 1
        elif lowered in WRITE_TOOLS:
            writes += 1
    return {"reads": reads, "writes": writes}


def _listing(document: AuditDocument) -> Dict[str, int]:
    skills = 0
    occurrences = 0
    reinject_occurrences = 0
    reinject_tokens = 0
    for skill in document.skills:
        listings = [o for o in skill.observations if o.kind == "listing"]
        if not listings:
            continue
        skills += 1
        occurrences += len(listings)
        # Observations are (ts, item_id, kind)-sorted by the builder: the
        # first listing is the catalog offer, every later one a re-offer.
        reinject_occurrences += len(listings) - 1
        reinject_tokens += sum(o.tokens_est for o in listings[1:])
    return {"skills": skills, "occurrences": occurrences,
            "reinject_occurrences": reinject_occurrences,
            "reinject_tokens_est": reinject_tokens}


def _inject(document: AuditDocument) -> Dict[str, int]:
    items = [i for i in document.items if i.bucket == "inject"]
    return {"items": len(items),
            "tokens_est": sum(i.tokens_est for i in items)}


def _interventions(document: AuditDocument) -> Dict[str, int]:
    rows = document.interventions
    return {"total": len(rows),
            "signal": sum(1 for r in rows if r.label != "noise")}


def process_signature(document: AuditDocument) -> dict:
    """The per-session process block: machinery features + style verdict."""
    workflow = _workflow(document)
    listing = _listing(document)
    inject = _inject(document)
    interventions = _interventions(document)
    loads = len(document.skill_loads)
    reloads = sum(1 for l in document.skill_loads if l.kind == "reload")

    touches = workflow["reads"] + workflow["writes"]
    if touches and loads:
        style = "pipeline"
    elif not touches and not loads:
        style = "direct"
    else:
        style = "mixed"

    notes = []
    if not listing["skills"]:
        notes.append(
            "no skill listings observed — listing re-injection is "
            "unmeasurable for this session shape")

    return {
        "style": style,
        "inject": inject,
        "listing": listing,
        "skill_loads": {"loads": loads, "reloads": reloads},
        "workflow": workflow,
        "interventions": interventions,
        "notes": notes,
    }
