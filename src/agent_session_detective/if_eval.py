"""Instruction Following evaluation, ported from AWS Skill Eval's design
(strands-agents/evals: skill_instruction_following_evaluator, prompt v0).

Each prescribed playbook step gets a ternary verdict anchored to trajectory
evidence (actions and results, never plans or claims). Coverage is recomputed
from the verdicts, not trusted from the model. The pass gate defaults to 0.75
(Mostly Followed): steps are prescriptive, so the bar sits above the median.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .judge import Judge
from .wire import Session

STATUS_WEIGHT = {"covered": 1.0, "partial": 0.5, "skipped": 0.0}
DEFAULT_GATE = 0.75

STEP_RE = re.compile(r"^(\d+)\.\s+(.*)$")
FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

PROMPT_TEMPLATE = """You are evaluating whether an agent followed the prescribed steps of a playbook while executing a session.

Playbook (harness metadata already stripped):
---
{playbook}
---

Prescribed steps (numbered):
{steps}

Session trajectory (user turns, then tool calls and results, truncated):
---
{trajectory}
---

For EACH numbered step above, judge its execution status as exactly one of: covered, partial, skipped.
Rules:
- Count only actions or results present in the trajectory. Plans, unexecuted code, and "claims of intent" do not count as covered.
- If the step prescribes a decision tree, judge only the branch this session should have taken; untouched branches are not skipped.
- "partial" means some but not all of the step's requirements are evidenced in the trajectory.
- Every non-skipped verdict must carry "evidence": a verbatim quote copied from the trajectory.
- A step the agent explicitly declined with a stated reason still counts as skipped (the reason may appear in your rationale).

Reply with a JSON array and nothing else, one object per step, in order:
[{{"index": 1, "status": "covered|partial|skipped", "evidence": "<verbatim quote or empty>", "rationale": "<one sentence>"}}]"""


@dataclass
class StepVerdict:
    step: str
    status: str  # covered | partial | skipped
    evidence: str
    rationale: str


@dataclass
class IFResult:
    playbook: str
    verdicts: List[StepVerdict] = field(default_factory=list)
    coverage: float = 0.0
    gate: float = DEFAULT_GATE
    not_applicable: bool = False  # playbook has no enumerable steps

    @property
    def passed(self) -> bool:
        return self.not_applicable or self.coverage >= self.gate


def strip_frontmatter(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1).strip()


def parse_playbook_steps(text: str) -> List[str]:
    """Numbered steps with their indented continuation lines, as single strings."""
    steps: List[str] = []
    current: Optional[List[str]] = None
    for line in strip_frontmatter(text).splitlines():
        match = STEP_RE.match(line)
        if match:
            if current:
                steps.append(" ".join(current))
            current = ["%s. %s" % (match.group(1), match.group(2).strip())]
        elif current is not None and (line.startswith("   ") or line.startswith("\t")):
            current.append(line.strip())
        elif current is not None and line.strip() == "":
            if current:
                steps.append(" ".join(current))
            current = None
    if current:
        steps.append(" ".join(current))
    return steps


def summarize_trajectory(session: Session, budget: int = 7000) -> str:
    lines: List[str] = []
    for event in session.events:
        if event.type in ("StepBegin", "StatusUpdate"):
            continue
        lines.append(event.text_preview(limit=150))
    text = "\n".join(lines)
    return text[:budget] + (" ..." if len(text) > budget else "")


def evaluate_playbook(
    judge: Judge, playbook_path: Path, session: Session, gate: Optional[float] = None
) -> IFResult:
    text = Path(playbook_path).read_text(encoding="utf-8", errors="replace")
    steps = parse_playbook_steps(text)
    result = IFResult(playbook=Path(playbook_path).name, gate=DEFAULT_GATE if gate is None else gate)
    if not steps:
        result.not_applicable = True
        return result
    trajectory = summarize_trajectory(session)
    numbered = "\n".join("%d. %s" % (i, s) for i, s in enumerate(steps, 1))
    prompt = PROMPT_TEMPLATE.format(
        playbook=strip_frontmatter(text)[:3000],
        steps=numbered,
        trajectory=trajectory,
    )
    try:
        raw = judge.ask(prompt)
    except Exception as exc:
        result.not_applicable = True
        result.verdicts = [
            StepVerdict(s, "skipped", "", "judge error: %s" % exc) for s in steps
        ]
        return result
    parsed = _parse_json_array(raw)
    by_index = {int(p.get("index")): p for p in parsed if isinstance(p, dict)}
    total = 0.0
    for i, step in enumerate(steps, 1):
        p = by_index.get(i) or {}
        status = str(p.get("status", "skipped")).lower().strip().strip('"').strip("'")
        if status not in STATUS_WEIGHT:
            status = "skipped"
        evidence = str(p.get("evidence") or "").strip()
        # Evidence must be verbatim from the trajectory, like trigger judging (R8).
        if status != "skipped" and evidence not in trajectory:
            status, evidence = "skipped", ""
        result.verdicts.append(
            StepVerdict(
                step=step,
                status=status,
                evidence=evidence,
                rationale=str(p.get("rationale") or ""),
            )
        )
        total += STATUS_WEIGHT[status]
    # Recompute coverage from verdicts; never trust the model's arithmetic.
    result.coverage = total / len(steps)
    return result


def _parse_json_array(text: str) -> list:
    match = re.search(r"\[.*\]", text.strip(), re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
