# Skill GAP Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 catalog（磁盘 SKILL.md 期望）× IR 实际观测 × judge 反事实判定接成结构化 GAP 报告（`skill_gaps`），进 CLI 报告与 web。

**Architecture:** 新纯函数模块 `ir/gap.py` 显式吃 IR state（skills+items）与 catalog、judgments；`judge_session` 的候选过滤权上移给调用方（IR 派生候选池）；CLI/web 在组装层并键 `skill_gaps`，`build_analyses` 纯度不动。

**Tech Stack:** Python 3 stdlib + unittest（项目无 pytest，测试命令用 `python -m unittest`）；报告为内联 HTML 字符串；web 面板为 `webapp/app.js` 字符串拼接。

**Spec:** `docs/superpowers/specs/2026-10-12-skill-gap-analysis-design.md`

**测试运行方式**：项目用 unittest。单文件：`python -m unittest tests.test_ir_gap -v`；全量：`python -m unittest discover -s tests -v`（在仓库根 `/Users/wangting/work/agent-session-detective` 下运行；src 布局由 pyproject 可编辑安装解析）。

---

### Task 1: `ir/gap.py` 核心 — 观测计数、join、集合、ir_only

**Files:**
- Create: `src/agent_session_detective/ir/gap.py`
- Test: `tests/test_ir_gap.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_ir_gap.py`：

```python
"""Skill GAP analysis unit tests — hand-built IR state, catalog, judgments.

Follows the test_ir_loads.py boundary: gap.py reads projected IR state
(SkillEntity + ContentItem), so join, set, and prefilter rules are tested
directly at that boundary, never through transcripts.
"""

import unittest
from pathlib import Path

from agent_session_detective.catalog import Skill
from agent_session_detective.ir.gap import gap_candidates, skill_gaps
from agent_session_detective.ir.schema import ContentItem, Observation, SkillEntity
from agent_session_detective.judge import Judgment


def make_skill(name, description="Use when testing."):
    return Skill(name=name, description=description,
                 trigger_excerpt=description,
                 path=Path("/tmp/%s/SKILL.md" % name))


def make_judgment(name, triggered=False, error=None):
    return Judgment(
        skill_name=name, triggered=triggered,
        turn=2 if triggered else None,
        rationale="session asked for %s" % name if triggered else "",
        evidence="please use %s" % name if triggered else "",
        confidence=0.9, error=error)


def entity(skill_id, kinds, aliases=(), tokens=100):
    """One observation per kind in `kinds`, wire order preserved."""
    observations = []
    for n, kind in enumerate(kinds, start=1):
        observations.append(Observation(
            kind=kind,
            channel="skill_listing" if kind == "listing" else "test",
            agent_id="main", call_id=None, ts=float(n),
            tokens_est=tokens,
            body_sha1="sha-%s-%d" % (skill_id, n) if kind == "body" else None,
            item_id="%s:%d" % (skill_id, n)))
    return SkillEntity(skill_id=skill_id, name=skill_id,
                       observations=observations, aliases=list(aliases))


class JoinAndSetsTest(unittest.TestCase):
    def test_join_sets_and_gap_categories(self):
        skills = [
            entity("alpha", ("listing", "body", "execution")),
            entity("beta", ("listing",)),
            entity("ghost", ("body",)),  # body without listing, no catalog entry
        ]
        catalog = [make_skill("alpha"), make_skill("beta")]
        gap = skill_gaps(skills, catalog, [])

        self.assertEqual(gap["sets"], {"listed": 2, "loaded": 2, "executed": 1})
        self.assertEqual(gap["join"]["matched"], 2)
        self.assertEqual(gap["join"]["catalog_only"], 0)
        self.assertEqual(
            [row["skill_id"] for row in gap["join"]["ir_only"]], ["ghost"])
        ir_only = gap["join"]["ir_only"][0]
        self.assertEqual(
            (ir_only["listed"], ir_only["loaded"], ir_only["executed"]),
            (0, 1, 0))
        self.assertEqual(
            [row["skill_id"] for row in gap["listed_unloaded"]], ["beta"])
        self.assertEqual(
            [row["skill_id"] for row in gap["loaded_unused"]], ["ghost"])

    def test_loaded_unused_carries_catalog_description(self):
        skills = [entity("alpha", ("body",))]
        catalog = [make_skill("alpha", description="Route here for pages.")]
        gap = skill_gaps(skills, catalog, [])
        row = gap["loaded_unused"][0]
        self.assertEqual(row["description"], "Route here for pages.")
        self.assertEqual((row["bodies"], row["stubs"]), (1, 0))

    def test_unmatched_loaded_unused_has_no_description(self):
        skills = [entity("ghost", ("body",))]
        gap = skill_gaps(skills, [], [])
        row = gap["loaded_unused"][0]
        self.assertIsNone(row["description"])
        self.assertIn("1 IR skill(s) matched no catalog entry", gap["notes"])

    def test_alias_joins_catalog(self):
        # identity merge (IR 1.2): bare id absorbed into namespaced twin
        skills = [entity("demo", ("listing", "body"), aliases=("pkg:demo",))]
        catalog = [make_skill("pkg:demo")]
        gap = skill_gaps(skills, catalog, [])
        self.assertEqual(gap["join"]["matched"], 1)
        self.assertEqual(gap["join"]["ir_only"], [])
        self.assertEqual(gap["loaded_unused"], [])

    def test_stub_counts_as_loaded(self):
        skills = [entity("beta", ("listing", "stub"))]
        gap = skill_gaps(skills, [make_skill("beta")], [])
        self.assertEqual(gap["listed_unloaded"], [])
        self.assertEqual(gap["loaded_unused"], [])

    def test_judge_unconfigured_notes_and_unjudged(self):
        skills = [entity("beta", ("listing",))]
        gap = skill_gaps(skills, [make_skill("beta")], [],
                         stats={"listed_unloaded": 1, "candidates": 0,
                                "prefiltered": 1},
                         judge_configured=False)
        self.assertTrue(gap["unjudged"]["judge_unconfigured"])
        self.assertEqual(gap["unjudged"]["count"], 1)
        self.assertIn("judge not configured", gap["notes"][0])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_ir_gap -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_session_detective.ir.gap'`

- [ ] **Step 3: 写最小实现**

创建 `src/agent_session_detective/ir/gap.py`：

```python
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

from typing import Dict, List, Sequence, Tuple

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
```

文件顶部 import 需要补 `Optional`：把第一行 typing import 改为
`from typing import Dict, List, Optional, Sequence, Tuple`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m unittest tests.test_ir_gap -v`
Expected: PASS（6 tests）

- [ ] **Step 5: 全量回归**

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK（无回归）

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/ir/gap.py tests/test_ir_gap.py
git commit -m "feat: skill GAP core — IR/catalog join, sets, gap categories"
```

---

### Task 2: `gap_candidates` 预过滤

**Files:**
- Modify: `src/agent_session_detective/ir/gap.py`（文件末尾追加）
- Test: `tests/test_ir_gap.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_ir_gap.py` 末尾追加：

```python
def make_item(seq, text=None):
    return ContentItem(
        item_id="main:%d" % seq, agent_id="main", bucket="user",
        kind="human_input", name=None, channel="qoder:user_input",
        wire_seq=seq, gone_seq=None,
        size_chars=len(text or ""), tokens_est=max(1, len(text or "") // 4),
        sha1="isha-%d" % seq, norm_sha1="inorm-%d" % seq,
        record={"file": "transcript.jsonl", "seq": seq},
        preview=(text or "")[:80], human_text=text)


class GapCandidatesTest(unittest.TestCase):
    def test_prefilter_keeps_only_mentioned_unloaded(self):
        skills = [
            entity("alpha", ("listing",)),   # unloaded, never mentioned
            entity("beta", ("listing",)),    # unloaded, mentioned below
            entity("gamma", ("listing", "body")),  # loaded — never a candidate
        ]
        items = [make_item(1, "please use beta for this")]
        catalog = [make_skill("alpha"), make_skill("beta"), make_skill("gamma")]

        pairs, stats = gap_candidates(skills, items, catalog)

        self.assertEqual([skill.name for _, skill in pairs], ["beta"])
        self.assertEqual(
            stats, {"listed_unloaded": 2, "candidates": 1, "prefiltered": 1})

    def test_alias_mention_qualifies(self):
        skills = [entity("demo", ("listing",), aliases=("pkg:demo",))]
        items = [make_item(1, "run pkg:demo please")]
        catalog = [make_skill("pkg:demo")]

        pairs, stats = gap_candidates(skills, items, catalog)

        self.assertEqual([skill.name for _, skill in pairs], ["pkg:demo"])
        self.assertEqual(stats["candidates"], 1)

    def test_mention_is_casefolded(self):
        skills = [entity("beta", ("listing",))]
        items = [make_item(1, "BETA would help here")]
        catalog = [make_skill("beta")]

        pairs, _ = gap_candidates(skills, items, catalog)
        self.assertEqual(len(pairs), 1)

    def test_unmatched_candidate_is_counted_not_judged(self):
        skills = [entity("ghost", ("listing",))]
        items = [make_item(1, "ghost ghost ghost")]
        catalog = []

        pairs, stats = gap_candidates(skills, items, catalog)

        self.assertEqual(pairs, [])
        self.assertEqual(
            stats, {"listed_unloaded": 1, "candidates": 0, "prefiltered": 1})

    def test_no_listing_means_no_candidate_even_if_mentioned(self):
        skills = [entity("alpha", ("body",))]
        items = [make_item(1, "alpha alpha")]
        catalog = [make_skill("alpha")]

        pairs, stats = gap_candidates(skills, items, catalog)

        self.assertEqual(pairs, [])
        self.assertEqual(stats["listed_unloaded"], 0)

    def test_prefiltered_skill_reaches_skill_gaps_unjudged(self):
        skills = [entity("alpha", ("listing",)), entity("beta", ("listing",))]
        items = [make_item(1, "only beta matters")]
        catalog = [make_skill("alpha"), make_skill("beta")]
        pairs, stats = gap_candidates(skills, items, catalog)

        # beta was judged (not triggered), alpha was prefiltered away:
        # unjudged = prefiltered(1) + over_limit(0); a False judgment is a
        # verdict, so it does not count as unjudged and not as missed.
        gap = skill_gaps(skills, catalog, [make_judgment("beta")],
                         stats=stats, judge_configured=True)

        self.assertEqual(gap["unjudged"]["prefiltered"], 1)
        self.assertEqual(gap["unjudged"]["count"], 1)
        self.assertEqual(gap["missed_triggers"], [])
        self.assertTrue(any("prefiltered" in n for n in gap["notes"]))
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_ir_gap.GapCandidatesTest -v`
Expected: FAIL — `ImportError: cannot import name 'gap_candidates'`

- [ ] **Step 3: 实现**

在 `src/agent_session_detective/ir/gap.py` 末尾追加：

```python
def _session_text(items: Sequence[ContentItem]) -> str:
    return "\n".join(
        item.human_text for item in items if item.human_text).casefold()


def gap_candidates(skills: Sequence[SkillEntity],
                   items: Sequence[ContentItem],
                   catalog: Sequence[Skill],
                   ) -> Tuple[List[Tuple[str, Skill]], dict]:
    """Listed-but-never-loaded skills whose name the session mentioned.

    The prefilter is deliberately cheap and legible: a candidate must have
    been offered to the agent (a listing), must never have loaded (no
    body/stub/execution), and its name or an alias must appear in the
    session's typed human input. Skills that fail the name test are
    counted in ``prefiltered`` — never silently dropped.
    """
    counts = _counts(skills)
    matched, _, _ = _join(counts, catalog)
    text = _session_text(items)
    pairs: List[Tuple[str, Skill]] = []
    prefiltered = 0
    listed_unloaded = 0
    for skill_id in sorted(counts):
        entry = counts[skill_id]
        if not entry["listed"] or entry["loaded"] or entry["executed"]:
            continue
        listed_unloaded += 1
        catalog_skill = matched.get(skill_id)
        if catalog_skill is None:
            prefiltered += 1
            continue
        keys = [skill_id.lower()] + [a.lower() for a in entry["aliases"]]
        if not any(k in text for k in keys):
            prefiltered += 1
            continue
        pairs.append((skill_id, catalog_skill))
    stats = {"listed_unloaded": listed_unloaded, "candidates": len(pairs),
             "prefiltered": prefiltered}
    return pairs, stats
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_ir_gap -v`
Expected: PASS（12 tests）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/gap.py tests/test_ir_gap.py
git commit -m "feat: gap candidate prefilter — listed-unloaded, name-mentioned"
```

---

### Task 3: `judge_session` 候选过滤权上移 + catalog 默认目录

**Files:**
- Modify: `src/agent_session_detective/judge.py:161-169`
- Modify: `src/agent_session_detective/catalog.py:22`
- Test: `tests/test_judge.py`（新建）、`tests/test_catalog.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_judge.py`：

```python
"""judge_session contract: candidates come in final — no internal filtering.

The counterfactual question only makes sense for skills the session never
loaded, and IR evidence decides that. judge_session must therefore trust
the caller's list instead of re-filtering through the older timeline view,
whose consumed set can disagree with IR observations.
"""

import unittest

from agent_session_detective.judge import Judgment, judge_session


class StubJudge:
    """Records what it was asked; never hits the network."""

    def __init__(self, model="stub"):
        self.model = model
        self.asked = []

    def judge_skill(self, skill, turns_block):
        self.asked.append(skill.name)
        return Judgment(skill_name=skill.name, triggered=False, turn=None,
                        rationale="", evidence="", confidence=0.9)


class FakeTurn:
    def text_preview(self, limit):
        return "turn text"


class FakeTimeline:
    """Duck-typed: has turns, deliberately has NO consumed_skill_names."""

    turns = [FakeTurn()]


def make_skill(name):
    from pathlib import Path
    from agent_session_detective.catalog import Skill
    return Skill(name=name, description="Use when %s." % name,
                 trigger_excerpt="Use when %s." % name,
                 path=Path("/tmp/%s/SKILL.md" % name))


class JudgeSessionCandidatesTest(unittest.TestCase):
    def test_asks_exactly_the_candidates_given(self):
        from agent_session_detective.judge import Judge  # noqa: F401
        judge = StubJudge()
        catalog = [make_skill("alpha"), make_skill("beta")]

        judgments = judge_session(FakeTimeline(), catalog, judge)

        self.assertEqual(judge.asked, ["alpha", "beta"])
        self.assertEqual(len(judgments), 2)

    def test_limit_truncates_candidates(self):
        judge = StubJudge()
        catalog = [make_skill("alpha"), make_skill("beta")]

        judge_session(FakeTimeline(), catalog, judge, limit=1)

        self.assertEqual(judge.asked, ["alpha"])
```

创建 `tests/test_catalog.py`：

```python
"""Catalog loader contracts that span features."""

import unittest


class DefaultDirsTest(unittest.TestCase):
    def test_qoder_skills_is_a_default_dir(self):
        from agent_session_detective.catalog import DEFAULT_SKILL_DIRS
        self.assertIn("~/.qoder/skills", DEFAULT_SKILL_DIRS)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_judge tests.test_catalog -v`
Expected: FAIL — `AttributeError: 'FakeTimeline' object has no attribute 'consumed_skill_names'`；`test_qoder_skills_is_a_default_dir` FAIL

- [ ] **Step 3: 实现**

`src/agent_session_detective/judge.py` — 替换 `judge_session`（161-169 行）：

```python
def judge_session(
    timeline: Timeline, skills: List[Skill], judge: Judge, limit: Optional[int] = None
) -> List[Judgment]:
    """Judge exactly the candidates given; the caller owns filtering.

    The counterfactual question only makes sense for skills the session
    never loaded, and IR evidence decides that — so this function trusts
    the caller's list instead of re-filtering through the older timeline
    view, whose consumed set can disagree with IR observations.
    """
    candidates = list(skills)
    if limit:
        candidates = candidates[:limit]
    turns_block = summarize_turns(timeline)
    return [judge.judge_skill(skill, turns_block) for skill in candidates]
```

`src/agent_session_detective/catalog.py:22` — 改为：

```python
DEFAULT_SKILL_DIRS = ("~/.agents/skills", "~/.kimi/skills", "~/.qoder/skills")
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_judge tests.test_catalog -v`
Expected: PASS（3 tests）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK（现有 judge 路径测试全部走 `--no-judge`，不受影响）

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/judge.py src/agent_session_detective/catalog.py tests/test_judge.py tests/test_catalog.py
git commit -m "feat: judge_session takes final candidates; Qoder skills dir default"
```

---

### Task 4: CLI 接线 + report GAP 节

**Files:**
- Modify: `src/agent_session_detective/cli.py`（import 区 + 主流程 184-192 行 + render_report 调用 232-242 行）
- Modify: `src/agent_session_detective/report.py`（`render_report` 签名 294-306 行；`# --- Instruction Following ---`（450 行）前插入 GAP 节）
- Test: `tests/test_cli.py`（追加）、`tests/test_report_injected.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli.py` 末尾追加（该文件已有 `FIXTURES`、`json`、`shutil`、`tempfile`、`Path`、`patch`、`cli` 的 import）：

```python
class SkillGapWiringTests(unittest.TestCase):
    def test_cli_passes_skill_gap_block_to_report(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "foo.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            gap = render.call_args.kwargs["gap"]
            self.assertEqual(
                sorted(gap),
                ["join", "listed_unloaded", "loaded_unused", "missed_triggers",
                 "notes", "sets", "unjudged"])
            self.assertTrue(gap["unjudged"]["judge_unconfigured"])
```

在 `tests/test_report_injected.py` 末尾追加：

```python
def gap_block():
    return {
        "join": {"matched": 1,
                 "ir_only": [{"skill_id": "ghost", "name": "ghost",
                              "listed": 3, "loaded": 0, "executed": 0}],
                 "catalog_only": 5},
        "sets": {"listed": 2, "loaded": 1, "executed": 1},
        "listed_unloaded": [
            {"skill_id": "ghost", "name": "ghost", "description": None,
             "bodies": 0, "stubs": 0}],
        "missed_triggers": [
            {"skill_id": "beta", "name": "beta", "turn": 2,
             "rationale": "session asked for beta",
             "evidence": "please use beta", "confidence": 0.9}],
        "loaded_unused": [
            {"skill_id": "alpha", "name": "alpha",
             "description": "Use when testing.", "bodies": 1, "stubs": 1}],
        "unjudged": {"count": 0, "prefiltered": 0, "over_limit": 0,
                     "judge_errors": 0, "judge_unconfigured": False},
        "notes": [],
    }


class SkillGapSectionTests(unittest.TestCase):
    def render_with_gap(self, gap):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "t.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            session = load_session(transcript)
            return render_report(
                session, build_timeline(session), [],
                judge_enabled=True, catalog_size=6, gap=gap)

    def test_section_renders_missed_triggers_with_evidence(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("Skill GAP", html)
        self.assertIn("missed triggers", html)
        self.assertIn("please use beta", html)
        self.assertIn("beta", html)

    def test_section_renders_loaded_unused_with_declared_use(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("loaded but never executed", html)
        self.assertIn("Use when testing.", html)

    def test_section_renders_ir_only_table(self):
        html = self.render_with_gap(gap_block())
        self.assertIn("IR-only (no catalog entry)", html)
        self.assertIn("ghost", html)

    def test_judge_off_notice_when_unconfigured(self):
        gap = gap_block()
        gap["unjudged"]["judge_unconfigured"] = True
        gap["missed_triggers"] = []
        html = self.render_with_gap(gap)
        self.assertIn("trigger GAPs not evaluated", html)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_cli.SkillGapWiringTests tests.test_report_injected.SkillGapSectionTests -v`
Expected: FAIL — CLI：`KeyError: 'gap'`（call_args.kwargs 无 gap）；report：`AssertionError: 'Skill GAP' not found`

- [ ] **Step 3: 实现 report.py**

3a. `render_report` 签名（report.py:294-306）在 `injected_skills` 后加参：

```python
def render_report(
    session: Session,
    timeline: Timeline,
    judgments: List[Judgment],
    judge_enabled: bool,
    catalog_size: int,
    expected: Optional[List[str]] = None,
    if_results: Optional[List["IFResult"]] = None,
    token_stats: Optional[TokenStats] = None,
    no_self_invoke: Optional[set] = None,
    document: Optional["AuditDocument"] = None,
    injected_skills: Optional[dict] = None,
    gap: Optional[dict] = None,
) -> str:
```

3b. 在 `    # --- Instruction Following ---`（450 行）之前插入：

```python
    # --- Skill GAP (expectation vs actual) ---
    # Three sources with different epistemic status: IR observations are
    # log facts, catalog wording is what the agent saw, judgments are LLM
    # output carried with verbatim evidence. The section never presents a
    # judgment as a fact.
    if gap is not None:
        join = gap["join"]
        sets = gap["sets"]
        parts.append("<h2>Skill GAP</h2>")
        parts.append(
            "<p class='dim'>catalog matched %d · IR-only %d · catalog-only %d"
            " · listed %d / loaded %d / executed %d</p>"
            % (join["matched"], len(join["ir_only"]), join["catalog_only"],
               sets["listed"], sets["loaded"], sets["executed"]))
        unjudged = gap["unjudged"]
        if unjudged["judge_unconfigured"]:
            parts.append(
                "<div class='notice'>Trigger judging is off — trigger GAPs "
                "not evaluated; structural GAPs below are still log facts.</div>")
        elif unjudged["count"] or unjudged["judge_errors"]:
            parts.append(
                "<p class='dim'>unjudged: %d (prefiltered %d · over limit %d)"
                " · judge defects %d</p>"
                % (unjudged["count"], unjudged["prefiltered"],
                   unjudged["over_limit"], unjudged["judge_errors"]))
        if gap["missed_triggers"]:
            parts.append("<h3>missed triggers</h3><table><tr><th>skill</th>"
                         "<th>turn</th><th>confidence</th><th>rationale</th>"
                         "<th>evidence (verbatim)</th></tr>")
            for m in gap["missed_triggers"]:
                parts.append(
                    "<tr><td>%s%s</td><td>%s</td><td>%.2f</td><td>%s</td>"
                    "<td><pre>%s</pre></td></tr>"
                    % (esc(m["name"]), injected_badge(m["name"]),
                       m["turn"] if m["turn"] is not None else "&mdash;",
                       m["confidence"], esc(m["rationale"]),
                       esc(m["evidence"])))
            parts.append("</table>")
        elif judge_enabled:
            parts.append("<p class='dim'>no missed triggers judged.</p>")
        if gap["loaded_unused"]:
            parts.append("<h3>loaded but never executed</h3>"
                         "<table><tr><th>skill</th><th>declared use</th>"
                         "<th>bodies</th><th>stubs</th></tr>")
            for row in gap["loaded_unused"]:
                parts.append(
                    "<tr><td>%s%s</td><td>%s</td><td>%d</td><td>%d</td></tr>"
                    % (esc(row["name"]), injected_badge(row["name"]),
                       esc(row["description"]) if row["description"]
                       else "<span class='meta'>no catalog entry</span>",
                       row["bodies"], row["stubs"]))
            parts.append("</table>")
        if join["ir_only"]:
            parts.append("<h3>IR-only (no catalog entry)</h3><table>"
                         "<tr><th>skill</th><th>listed</th><th>loaded</th>"
                         "<th>executed</th></tr>")
            for row in join["ir_only"]:
                parts.append(
                    "<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td></tr>"
                    % (esc(row["skill_id"]), row["listed"], row["loaded"],
                       row["executed"]))
            parts.append("</table>")
        for note in gap["notes"]:
            parts.append("<p class='dim'>%s</p>" % esc(note))
```

- [ ] **Step 4: 实现 cli.py**

4a. import 区（22 行 `from .ir import build_analyses, build_audit_document` 之后）加：

```python
from .ir.gap import gap_candidates, skill_gaps
```

4b. 把 184-192 行的 judge 块替换为：

```python
    candidate_pairs, gap_stats = gap_candidates(
        document.skills, document.items, catalog)
    judge = None if args.no_judge else Judge.from_env()
    judgments = []
    if judge:
        n = (min(args.judge_limit, len(candidate_pairs))
             if args.judge_limit is not None else len(candidate_pairs))
        print("Judging %d gap candidates (model: %s)..." % (n, judge.model),
              file=sys.stderr)
        judgments = judge_session(
            timeline, [skill for _, skill in candidate_pairs], judge,
            limit=args.judge_limit)
    else:
        print("Judge not configured; facts only. See ASD_JUDGE_* env vars.", file=sys.stderr)
    gap = skill_gaps(
        document.skills, catalog, judgments, stats=gap_stats,
        judge_configured=judge is not None)
```

4c. `render_report(...)` 调用（232-242 行）在 `injected_skills=...` 后加一行：

```python
        gap=gap,
```

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_cli.SkillGapWiringTests tests.test_report_injected -v`
Expected: PASS

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/cli.py src/agent_session_detective/report.py tests/test_cli.py tests/test_report_injected.py
git commit -m "feat: wire skill GAP analysis into CLI audit and report"
```

---

### Task 5: web 接线（payload + 判定候选池）

**Files:**
- Modify: `src/agent_session_detective/web.py`（`run_audit`：938-944 行判定块、969-977 行 IR 构建块、991-994 行 rollup 区、1014+ 行 `job.result`）
- Test: `tests/test_web.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_web.py` 末尾追加（沿用文件内已有的 `web`、`tempfile`、`Path` import）：

```python
class RunAuditSkillGapTests(unittest.TestCase):
    def _transcript(self, directory):
        transcript = Path(directory) / "s.jsonl"
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    def test_result_carries_skill_gap_block(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            gap = job.result["skill_gaps"]
            self.assertEqual(
                sorted(gap),
                ["join", "listed_unloaded", "loaded_unused", "missed_triggers",
                 "notes", "sets", "unjudged"])
            self.assertIsInstance(gap["join"]["matched"], int)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_web.RunAuditSkillGapTests -v`
Expected: FAIL — `KeyError: 'skill_gaps'`

- [ ] **Step 3: 实现**

web.py `run_audit` 内三处改动：

3a. 把 938-944 行（`judge = Judge.from_env()` 到 `judgments = judge_session(...)`）替换为（IR 文档前移 + IR 派生候选池）：

```python
        # The IR document is built before judging: the gap candidate pool
        # (listed but never loaded, by IR evidence) is what the judge sees.
        job.mark("building IR document")
        from .ir.analyses import skill_loads as skill_loads_rollup
        from .ir.analyses import subagent_returns as subagent_returns_rollup
        from .ir.builder import build_audit_document
        from .ir.gap import gap_candidates, skill_gaps
        adapter_id = _ir_adapter_id(session, source_path)
        document = build_audit_document(session, adapter_id)
        candidate_pairs, gap_stats = gap_candidates(
            document.skills, document.items, catalog)
        judge = Judge.from_env()
        judgments: List[Judgment] = []
        judge_enabled = judge is not None
        if judge_enabled and job.params.get("judge_triggers"):
            job.mark("judging %d gap candidates (%s)"
                     % (len(candidate_pairs), judge.model))
            judgments = judge_session(
                timeline, [skill for _, skill in candidate_pairs], judge)
```

3b. 删除原 969-977 行的 IR 构建块及其注释（`# The IR document is built here so the result carries the` 到 `document = build_audit_document(session, adapter_id)`），其中 `from .ir.analyses import ...`、`from .ir.builder import ...`、`adapter_id = ...` 均已上移到 3a，此处只删不添。

3c. 在 `returns_block = subagent_returns_rollup(document)`（原 994 行）之后加：

```python
        gap_block = skill_gaps(
            document.skills, catalog, judgments, stats=gap_stats,
            judge_configured=judge_enabled)
```

3d. `job.result` 字典里 `"subagent_returns": returns_block,` 之后加：

```python
            "skill_gaps": gap_block,
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_web -v 2>&1 | tail -5`
Expected: PASS（含原有测试）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: web audit carries skill_gaps; judge sees IR-derived candidates"
```

---

### Task 6: webapp GAP 面板

**Files:**
- Modify: `src/agent_session_detective/webapp/app.js`（HELP 字典 81 行 `"bill"` 键后；渲染区 1229 行 skill_loads 块后）

- [ ] **Step 1: 写失败校验**

本任务无独立 JS 测试设施；用语法校验 + 既有 web 测试守护：

Run: `node --check src/agent_session_detective/webapp/app.js`
Expected: 无输出（语法通过）——先确认改动前基线如此

- [ ] **Step 2: HELP 字典加 "gaps" 键**

在 `app.js` HELP 对象的 `"bill": {...}` 条目（81 行）后追加（注意前一条目末尾补逗号）：

```js
    "gaps":
      "SKILL.md 期望 × IR 实际的差距。missed triggers = LLM 反事实判定「该触发却没触发」" +
      "（是判断不是事实，证据须逐字可核对）；loaded but never executed = 正文进了上下文却从未执行；" +
      "IR-only = 会话里出现但磁盘 catalog 找不到的 skill（版本或命名漂移信号）。" +
      "unjudged 计数保持透明：预过滤剔除与超限未判的都不冒充已判。"
```

- [ ] **Step 3: 渲染 GAP 面板**

在渲染区 `parts.push(renderTokenStats(r.token_stats));`（1231 行）之前插入：

```js
    // Skill GAP: SKILL.md expectation vs IR actual; judgments are LLM output
    // carried with verbatim evidence — never presented as facts
    if (r.skill_gaps) {
      var g = r.skill_gaps;
      var gapCards = g.missed_triggers.map(function (m) {
        return "<details><summary>" + esc(m.name) +
          " <span class='dim'>turn " + (m.turn == null ? "—" : m.turn) +
          " · confidence " + Number(m.confidence).toFixed(2) + "</span></summary>" +
          "<p>" + esc(m.rationale) + "</p><pre>" + esc(m.evidence) + "</pre></details>";
      }).join("");
      var unusedRows = g.loaded_unused.map(function (row) {
        return "<tr><td>" + esc(row.name) + "</td><td>" +
          (row.description ? esc(row.description) : "<span class='dim'>no catalog entry</span>") +
          "</td><td>" + row.bodies + "</td><td>" + row.stubs + "</td></tr>";
      }).join("");
      var irOnlyRows = g.join.ir_only.map(function (row) {
        return "<tr><td>" + esc(row.skill_id) + "</td><td>" + row.listed +
          "</td><td>" + row.loaded + "</td><td>" + row.executed + "</td></tr>";
      }).join("");
      var unj = g.unjudged;
      parts.push(
        "<section id='gaps'><h2>skill gaps " + helpDot("gaps") + "</h2>" +
        "<p class='dim'>catalog matched " + g.join.matched + " · IR-only " + g.join.ir_only.length +
        " · catalog-only " + g.join.catalog_only + " · listed " + g.sets.listed +
        " / loaded " + g.sets.loaded + " / executed " + g.sets.executed + "</p>" +
        (unj.judge_unconfigured
          ? "<p class='dim'>judge off — trigger GAPs not evaluated.</p>"
          : ((unj.count || unj.judge_errors)
            ? "<p class='dim'>unjudged " + unj.count + " (prefiltered " + unj.prefiltered +
              " · over limit " + unj.over_limit + ") · judge defects " + unj.judge_errors + "</p>"
            : "")) +
        (gapCards ? "<h3>missed triggers</h3>" + gapCards : "") +
        (unusedRows ? "<h3>loaded but never executed</h3><table><tr><th>skill</th><th>declared use</th><th>bodies</th><th>stubs</th></tr>" + unusedRows + "</table>" : "") +
        (irOnlyRows ? "<h3>IR-only (no catalog entry)</h3><table><tr><th>skill</th><th>listed</th><th>loaded</th><th>executed</th></tr>" + irOnlyRows + "</table>" : "") +
        g.notes.map(function (n) { return "<p class='dim'>" + esc(n) + "</p>"; }).join("") +
        "</section>");
    }
```

- [ ] **Step 4: 语法校验 + 全量回归**

Run: `node --check src/agent_session_detective/webapp/app.js`
Expected: 无输出

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: 手动验证（浏览器）**

启动 web：`python -m agent_session_detective.web`（或项目 README 中的启动命令），任选一个会话跑一次审计，确认 GAP 面板渲染、helpDot 弹出 "gaps" 文案、`--no-judge` 路径显示 "judge off" 提示。若无法起浏览器环境，明确报告未做此步。

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/webapp/app.js
git commit -m "feat: skill gaps panel in web UI"
```

---

## 完成定义

- `python -m unittest discover -s tests` 全绿
- 真实会话 CLI 跑通：`asd <transcript> --no-judge --skills-dir ~/.qoder/plugins/cache`，报告含 Skill GAP 节（join/sets、loaded_unused、ir_only、unjudged 透明计数）
- 配好 `ASD_JUDGE_*` 后 judge 只判候选池（stderr 显示 "Judging N gap candidates"）
- web payload 含 `skill_gaps` 键，UI 面板渲染
