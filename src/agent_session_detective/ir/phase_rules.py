# src/agent_session_detective/ir/phase_rules.py
"""Phase recognition rules for dispatch briefs (IR 1.1).

Landing path ③ (calibration) is OUT of scope: this rule set ships EMPTY by
design. It is calibrated from a real coding-v2 session in a later pass, at
which point ``RULE_SET_VERSION`` is set and a corpus fingerprint is recorded
in the coverage ``phase_recognition`` block. Until then every non-signature
brief classifies tier C (``phase_id: null``) — a phase attribution without a
rule match is never fabricated.

Rule shape (documented for the calibration pass):

    RULES = [
        {
            "phase_id": "phase-4a-coding",
            "name": "Phase 4a: coding",
            "patterns": [
                # target: "brief" matches the brief text;
                #         "read"  matches a Read tool_call file_path.
                # ref_kind ∈ {skill_file, phase_file, dir, unknown} — the
                # phase_refs vocabulary (coverage states which kind of
                # evidence matched, never just "matched").
                {"regex": r"phase[-_ ]?4a", "target": "brief",
                 "ref_kind": "unknown"},
                {"regex": r"\\.*/skills/.*/SKILL\\.md$", "target": "read",
                 "ref_kind": "skill_file"},
                {"regex": r"\\.*/src/phase_4a/", "target": "read",
                 "ref_kind": "phase_file"},
            ],
        },
        ...
    ]

Matching is ``re.search`` over the decoded text (brief text or Read
``file_path``); every match appends a phase ref
``{"ref", "kind", "source", "offset", "phase_id"}`` via :func:`match_refs`,
where ``source`` names which text matched ("brief" or "read:<path>").
"""

from __future__ import annotations

import re
from typing import List, Optional

RULE_SET_VERSION: Optional[str] = None  # null until landing path ③ calibration

CORPUS_NOTE = (
    "rule set empty: phase patterns await calibration from a real "
    "coding-v2 session (landing path 3); every non-signature dispatch "
    "classifies tier C with phase_id null until then"
)

RULES: List[dict] = []


def match_refs(rules: List[dict], text: str, source: str, target: str) -> List[dict]:
    """All rule matches of ``text`` as phase refs.

    ``target`` selects patterns ("brief" or "read"); ``source`` names the
    evidence source ("brief" or "read:<path>") and is carried on each ref
    verbatim. Order follows the rule list, then match offset.
    """
    refs: List[dict] = []
    for rule in rules:
        phase_id = rule.get("phase_id")
        for pattern in rule.get("patterns", []):
            regex = pattern.get("regex")
            if not isinstance(regex, str):
                continue
            if pattern.get("target") != target:
                continue
            kind = pattern.get("ref_kind")
            if kind not in ("skill_file", "phase_file", "dir", "unknown"):
                continue
            match = re.search(regex, text)
            if match is None:
                continue
            refs.append({
                "ref": match.group(0),
                "kind": kind,
                "source": source,
                "offset": match.start(),
                "phase_id": phase_id,
            })
    return refs


def rule_name(rules: List[dict], phase_id: str) -> Optional[str]:
    """Display name of the first rule with ``phase_id`` (facts only; None
    when the id comes from tier A, where the skill name is used)."""
    for rule in rules:
        if rule.get("phase_id") == phase_id:
            name = rule.get("name")
            return name if isinstance(name, str) and name else None
    return None
