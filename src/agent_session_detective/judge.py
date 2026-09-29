"""Counterfactual trigger judging via an OpenAI-compatible chat endpoint.

For each skill that never loaded, the judge sees the conversation and the
skill's trigger wording, and answers: at which decision point should this
skill have been triggered? Every judgment must carry verbatim evidence and a
confidence, or it is discarded as a tool defect (R7, R8).
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

from .catalog import Skill
from .timeline import Timeline

ENV_BASE_URL = "ASD_JUDGE_BASE_URL"
ENV_API_KEY = "ASD_JUDGE_API_KEY"
ENV_MODEL = "ASD_JUDGE_MODEL"

PROMPT_TEMPLATE = """You are auditing an AI coding agent session. The agent has a library of skills (instruction files it can load when relevant). A skill loads only when the agent explicitly invokes it.

Below is the session's user turns in order (truncated), followed by one skill that was NEVER loaded in this session.

{turns}

Skill name: {name}
Skill trigger wording (what the agent sees when deciding):
---
{trigger}
---

Question: at any point in this session, should the agent have triggered (loaded) this skill? Reply with a JSON object and nothing else:
{{"triggered": true/false, "turn": <1-based turn number or null>, "rationale": "<one or two sentences>", "evidence": "<verbatim quote from the session justifying the judgment>", "confidence": <0.0-1.0>}}
Rules: "evidence" must be a verbatim quote copied from the turns above, not a paraphrase. If triggered is false, turn is null and evidence may be empty. Calibrate strictly: most skills should be false. Answer true only when the session clearly calls for this specific skill and a competent agent would be expected to load it, not merely because the topic is vaguely related. Generic principles or advice skills count as triggered only when the session is directly and explicitly about that principle."""


@dataclass
class Judgment:
    skill_name: str
    triggered: bool
    turn: Optional[int]
    rationale: str
    evidence: str
    confidence: float
    error: Optional[str] = None


class Judge:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> Optional["Judge"]:
        base_url = os.environ.get(ENV_BASE_URL)
        api_key = os.environ.get(ENV_API_KEY)
        model = os.environ.get(ENV_MODEL)
        if not api_key:
            # Fallback: DeepSeek credentials, if present, are a working default.
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if api_key:
                base_url = base_url or "https://api.deepseek.com/v1"
                model = model or "deepseek-flash"
        if not api_key or not model:
            return None
        return cls(base_url=base_url or "https://api.openai.com/v1", api_key=api_key, model=model)

    def ask(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    def judge_skill(self, skill: Skill, turns_block: str) -> Judgment:
        prompt = PROMPT_TEMPLATE.format(
            turns=turns_block, name=skill.name, trigger=skill.trigger_excerpt[:1500]
        )
        try:
            raw = self.ask(prompt)
        except Exception as exc:  # network/auth/parse errors surface as judgment errors
            return Judgment(skill.name, False, None, "", "", 0.0, error=str(exc))
        parsed = _parse_json_object(raw)
        if parsed is None:
            return Judgment(skill.name, False, None, "", "", 0.0, error="unparseable judge reply")
        triggered = bool(parsed.get("triggered"))
        evidence = str(parsed.get("evidence") or "").strip()
        # A positive judgment without verbatim evidence is a defect, not a finding (R8).
        if triggered and not evidence:
            return Judgment(
                skill.name, False, None, "", "", 0.0,
                error="judge claimed trigger without evidence",
            )
        if triggered and evidence not in turns_block:
            return Judgment(
                skill.name, False, None, "", "", 0.0,
                error="judge evidence is not a verbatim quote",
            )
        return Judgment(
            skill_name=skill.name,
            triggered=triggered,
            turn=parsed.get("turn") if triggered else None,
            rationale=str(parsed.get("rationale") or ""),
            evidence=evidence,
            confidence=float(parsed.get("confidence") or 0.0),
        )


def _parse_json_object(text: str) -> Optional[dict]:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def summarize_turns(timeline: Timeline, per_turn_limit: int = 600) -> str:
    """Compact, verbatim-preserved rendering of user turns for the judge."""
    lines = []
    for i, turn in enumerate(timeline.turns, 1):
        text = turn.text_preview(limit=10_000)
        if len(text) > per_turn_limit:
            text = text[:per_turn_limit] + " ..."
        lines.append("Turn %d: %s" % (i, text))
    return "\n\n".join(lines) if lines else "(no user turns found)"


def judge_session(
    timeline: Timeline, skills: List[Skill], judge: Judge, limit: Optional[int] = None
) -> List[Judgment]:
    consumed = set(timeline.consumed_skill_names())
    candidates = [s for s in skills if s.name not in consumed]
    if limit:
        candidates = candidates[:limit]
    turns_block = summarize_turns(timeline)
    return [judge.judge_skill(skill, turns_block) for skill in candidates]
