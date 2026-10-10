# Bill Summary (P0-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the "bill" summary card to the ASD web report — six cost rows (with per-row honesty grades and in-section anchors), a routing bill (did the right things load), an optional billed block, and subagent return-bloat detection — per `docs/superpowers/specs/2026-10-10-bill-summary-design.md`.

**Architecture:** A new pure function `bill.py::build_bill` composes already-serialized blocks (token stats, skill loads, subagent returns, expectations/judgments/IF summaries, billed series) into the bill dict; `ir/analyses.py::subagent_returns` adds the evidence ledger it consumes. The web layer (`web.py::run_audit`) owns every join and adds `bill` + `subagent_returns` payload keys; the webapp renders one new section at the top of the report. No CLI surface change.

**Tech Stack:** stdlib-only Python (no new dependencies), `python -m pytest tests/ -q`, vanilla JS + CSS in `src/agent_session_detective/webapp/`.

---

## Guardrails (read before every task)

1. **Another concurrent session is editing this repo.** At plan-writing time the working tree carries its *uncommitted* sidebar-search WIP in `README.md`, `src/agent_session_detective/web.py`, `src/agent_session_detective/webapp/app.js`, `src/agent_session_detective/webapp/index.html`, `src/agent_session_detective/webapp/style.css`, `tests/test_web.py`. Rules:
   - Never run `git add -A` or `git add .` — stage files by exact path only.
   - Before staging any file that was already dirty at task start, run `git diff <file>` and confirm every hunk belongs to this plan. If unrelated (sidebar-search) hunks are present, STOP and ask the user before committing.
   - Make all edits content-anchored (unique surrounding text), never line-number based — the concurrent session may shift lines between tasks. If an anchor in this plan no longer matches, re-locate the symbol by searching for it; do not blind-edit.
2. **The ASD server on port 8471 belongs to another session — never kill or restart it.** All UI verification uses a separate port (8472).
3. Never push. Dual-remote pushes (origin + gitlab) happen only when the user asks.
4. EST/observed numbers are never divided by billed denominators; shares are computed only within the EST subtotal (`context_growth + repeat_tax + skill_load_cost + subagent_returns`). No volume → row omitted, never zero-filled. `None` means "no evidence", rendered as `unavailable`.
5. Baseline before Task 1: `python -m pytest tests/ -q` → **479 passed**. After Task 1 (5 new tests): 484. After Task 2 (9 new tests): 493. After Task 3 (1 new, 2 renamed): **494**.

---

### Task 0: Pre-flight

**Files:** none (verification only)

- [ ] **Step 1: Confirm the working tree state**

Run: `git status --porcelain`
Expected: the concurrent session's six dirty files (README.md, web.py, app.js, index.html, style.css, test_web.py), possibly more or fewer — record what you see; this list is the baseline for the commit guards in Tasks 3-5.

- [ ] **Step 2: Confirm the test baseline**

Run: `python -m pytest tests/ -q`
Expected: `479 passed` in ~2s. If the number differs (the concurrent session may have landed tests), note the new baseline and adjust the expected counts in guardrail 5 by the same delta.

- [ ] **Step 3: Confirm the spec is present**

Run: `ls docs/superpowers/specs/2026-10-10-bill-summary-design.md`
Expected: the file exists (committed as 25b6495). Read its "成本行等级" table before implementing Task 2 — it is the authority for row grades.

---

### Task 1: `subagent_returns` evidence ledger (IR)

**Files:**
- Modify: `src/agent_session_detective/ir/analyses.py` (import line, `RE_INJECTION_BUCKETS` block, between `dispatches()` and `build_analyses()`)
- Test: `tests/test_ir_analyses.py` (extend one import, add two imports, append a class before the `unittest.main()` block)

- [ ] **Step 1: Write the failing tests**

In `tests/test_ir_analyses.py`, change the import line:

```python
from agent_session_detective.ir.analyses import build_analyses
```

to:

```python
from agent_session_detective.ir.analyses import build_analyses, subagent_returns
```

and directly below `from agent_session_detective.wire import load_session` add:

```python
from tests.ir_helpers import IREventsTestCase
from tests.test_ir_dispatch import _clock, assistant_record, user_record
```

Then, **before** the trailing block

```python
if __name__ == "__main__":
    unittest.main()
```

append:

```python
def result_record(ts, uuid, tool_id, content, parent=None):
    rec = {"type": "user", "uuid": uuid, "parentUuid": parent,
           "isSidechain": False, "timestamp": ts,
           "message": {"content": [
               {"type": "tool_result", "tool_use_id": tool_id,
                "content": content}]}}
    return rec


class SubagentReturnsTest(IREventsTestCase, unittest.TestCase):
    def audit(self, main_lines, subagents=None):
        document = build_audit_document(
            self.load_lines(main_lines, subagents=subagents), "qoder")
        return subagent_returns(document)

    def test_full_join_return_cost_echo_and_ratio(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "please investigate the bug"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00A", "Agent",
                          {"subagent_type": "code",
                           "description": "spawned worker"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00A", "A" * 8004,
                          parent="m2"),
            assistant_record(clock(), "m4", "done", request_id="req-2"),
        ]
        worker = [
            user_record(clock(), "w1", "alpha-flow investigate the bug",
                        parent_tool="call_00A"),
            assistant_record(
                clock(), "w2",
                tool_use=("wread", "Read", {"file_path": "/tmp/bug.txt"}),
                request_id="req-w1"),
            result_record(clock(), "w3", "wread", "A" * 8004, parent="w2"),
            assistant_record(clock(), "w4", "all clear", request_id="req-w2"),
        ]
        document = build_audit_document(
            self.load_lines(main, subagents={"worker": worker}), "qoder")
        ledger = subagent_returns(document)

        self.assertEqual(len(ledger["rows"]), 1)
        row = ledger["rows"][0]
        self.assertEqual(row["dispatch_id"], "main:dispatch:1")
        self.assertEqual(row["subagent_agent_id"], "subagent:worker")
        self.assertEqual(row["description"], "spawned worker")
        self.assertEqual(row["return_tokens_est"], 2001)
        sub_total = sum(item.tokens_est for item in document.items
                        if item.agent_id == "subagent:worker")
        self.assertEqual(row["subagent_tokens_est"], sub_total)
        echo_item = next(item for item in document.items
                         if item.agent_id == "subagent:worker"
                         and item.kind == "tool_result")
        self.assertEqual(row["echo_item_id"], echo_item.item_id)
        self.assertEqual(row["ratio"], round(2001 / sub_total, 4))
        self.assertEqual(row["flagged_reasons"], ["size", "echo"])
        self.assertEqual(ledger["totals"], {
            "dispatches": 1, "linked": 1, "return_tokens_total": 2001,
            "flagged": 1, "process_unavailable": 0})

    def test_orphan_dispatches_have_process_unavailable_and_no_ratio(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "go"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00C", "Agent",
                          {"subagent_type": "code", "description": "one"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00C", "A" * 8004,
                          parent="m2"),
            assistant_record(
                clock(), "m4",
                tool_use=("call_00D", "Agent",
                          {"subagent_type": "code", "description": "two"}),
                request_id="req-2", parent="m3"),
            result_record(clock(), "m5", "call_00D", "ok", parent="m4"),
            assistant_record(clock(), "m6", "done", request_id="req-3"),
        ]
        ledger = self.audit(main)

        self.assertEqual([r["dispatch_id"] for r in ledger["rows"]],
                         ["main:dispatch:1", "main:dispatch:2"])
        first, second = ledger["rows"]
        self.assertIsNone(first["subagent_agent_id"])
        self.assertEqual(first["return_tokens_est"], 2001)
        self.assertIsNone(first["subagent_tokens_est"])
        self.assertIsNone(first["ratio"])
        self.assertEqual(first["flagged_reasons"], ["size"])
        self.assertEqual(second["return_tokens_est"], 1)
        self.assertEqual(second["flagged_reasons"], [])
        self.assertEqual(ledger["totals"], {
            "dispatches": 2, "linked": 0, "return_tokens_total": 2002,
            "flagged": 1, "process_unavailable": 2})

    def test_echo_only_matches_the_linked_subagent(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "please investigate"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00A", "Agent",
                          {"subagent_type": "code",
                           "description": "spawned joined"}),
                request_id="req-1", parent="m1"),
            result_record(clock(), "m3", "call_00A", "A" * 8004,
                          parent="m2"),
            assistant_record(clock(), "m4", "done", request_id="req-2"),
        ]
        subagents = {
            "joined": [
                user_record(clock(), "j1", "small brief",
                            parent_tool="call_00A"),
                assistant_record(clock(), "j2", "small reply",
                                 request_id="req-j1"),
            ],
            # claims a nonexistent parent call: its items exist in the
            # document but no dispatch row points at it
            "bystander": [
                user_record(clock(), "b1", "A" * 8004,
                            parent_tool="call_missing"),
            ],
        }
        document = build_audit_document(
            self.load_lines(main, subagents=subagents), "qoder")
        ledger = subagent_returns(document)

        self.assertEqual(len(ledger["rows"]), 1)
        row = ledger["rows"][0]
        self.assertEqual(row["subagent_agent_id"], "subagent:joined")
        self.assertIsNone(row["echo_item_id"])
        self.assertEqual(row["flagged_reasons"], ["size"])
        self.assertEqual(ledger["totals"]["process_unavailable"], 0)

    def test_bloat_threshold_is_inclusive(self):
        clock = _clock()
        main = [
            user_record(clock(), "m1", "go"),
            assistant_record(
                clock(), "m2",
                tool_use=("call_00C", "Agent",
                          {"subagent_type": "code", "description": "one"}),
                request_id="req-1", parent="m1"),
            # "A" * 8000 → estimate_tokens = 2000 → at threshold → flagged
            result_record(clock(), "m3", "call_00C", "A" * 8000,
                          parent="m2"),
            assistant_record(
                clock(), "m4",
                tool_use=("call_00D", "Agent",
                          {"subagent_type": "code", "description": "two"}),
                request_id="req-2", parent="m3"),
            # "A" * 7996 → 1999 → below threshold → not flagged
            result_record(clock(), "m5", "call_00D", "A" * 7996,
                          parent="m4"),
        ]
        ledger = self.audit(main)

        self.assertEqual(ledger["rows"][0]["flagged_reasons"], ["size"])
        self.assertEqual(ledger["rows"][1]["flagged_reasons"], [])

    def test_session_without_dispatches_returns_empty_ledger(self):
        ledger = self.audit([user_record(_clock()(), "m1", "hello")])

        self.assertEqual(ledger["rows"], [])
        self.assertEqual(ledger["totals"], {
            "dispatches": 0, "linked": 0, "return_tokens_total": 0,
            "flagged": 0, "process_unavailable": 0})


```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ir_analyses.py -q`
Expected: **FAIL** — `ImportError: cannot import name 'subagent_returns'`.

- [ ] **Step 3: Implement `subagent_returns`**

In `src/agent_session_detective/ir/analyses.py`, change the schema import:

```python
from .schema import AuditDocument
```

to:

```python
from .schema import AuditDocument, ContentItem
```

Directly below `RE_INJECTION_BUCKETS = ("inject", "skill")` add:

```python
# Return-bloat flagging (P0-1 bill summary): a subagent return at or
# above this many EST tokens is flagged "size" — the main agent paid
# for bulk where a summary would have done.
RETURN_BLOAT_TOKENS = 2000
```

Then, between the end of `dispatches()` (the `}` closing its return, right after the `"phase_recognition": ...` line) and `def build_analyses(document: AuditDocument) -> dict:`, insert:

```python
def subagent_returns(document: AuditDocument) -> dict:
    """Return-value bloat ledger for subagent dispatches.

    For every dispatch row: the parent-side tool_result tokens (what the
    main agent actually paid to receive), the subagent's whole-process
    tokens when its transcript is linked, an echo link (result text
    appears verbatim inside the subagent process — norm_sha1 equality),
    and a bloat flag (>= RETURN_BLOAT_TOKENS or an echo). Orphan
    dispatches keep ``subagent_tokens_est: None`` and count in
    ``process_unavailable`` — never guessed.
    """
    returns_by_tool: Dict[str, ContentItem] = {}
    for item in document.items:
        if item.kind == "tool_result" and item.tool_use_id is not None:
            returns_by_tool.setdefault(item.tool_use_id, item)
    sub_items: Dict[str, List[ContentItem]] = {}
    for item in document.items:
        if item.agent_id.startswith("subagent:"):
            sub_items.setdefault(item.agent_id, []).append(item)
    rows = []
    linked = 0
    flagged = 0
    process_unavailable = 0
    return_total = 0
    for dispatch in document.dispatches:
        return_item = returns_by_tool.get(dispatch.tool_use_id)
        return_tokens = (return_item.tokens_est
                          if return_item is not None else None)
        subagent_tokens = None
        echo_item_id = None
        if dispatch.subagent_agent_id is not None:
            linked += 1
            items = sub_items.get(dispatch.subagent_agent_id) or []
            if items:
                subagent_tokens = sum(item.tokens_est for item in items)
                if return_item is not None:
                    for item in items:
                        if item.norm_sha1 == return_item.norm_sha1:
                            echo_item_id = item.item_id
                            break
            else:
                process_unavailable += 1
        else:
            process_unavailable += 1
        ratio = (round(return_tokens / subagent_tokens, 4)
                 if return_tokens and subagent_tokens else None)
        reasons = []
        if return_tokens is not None and return_tokens >= RETURN_BLOAT_TOKENS:
            reasons.append("size")
        if echo_item_id is not None:
            reasons.append("echo")
        if reasons:
            flagged += 1
        if return_tokens is not None:
            return_total += return_tokens
        rows.append({
            "dispatch_id": dispatch.dispatch_id,
            "subagent_agent_id": dispatch.subagent_agent_id,
            "description": dispatch.description,
            "return_item_id": (return_item.item_id
                               if return_item is not None else None),
            "return_tokens_est": return_tokens,
            "subagent_tokens_est": subagent_tokens,
            "echo_item_id": echo_item_id,
            "ratio": ratio,
            "flagged_reasons": reasons,
        })
    return {"rows": rows, "totals": {
        "dispatches": len(document.dispatches), "linked": linked,
        "return_tokens_total": return_total, "flagged": flagged,
        "process_unavailable": process_unavailable}}
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_ir_analyses.py -q`
Expected: **PASS** (all classes in the file, including the 5 new tests).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: **484 passed** (baseline 479 + 5).

- [ ] **Step 6: Commit**

First verify only this task's files are staged (guardrail 1 — `tests/test_ir_analyses.py` and `src/agent_session_detective/ir/analyses.py` were NOT dirty at baseline, so a plain diff check suffices):

```bash
git diff --stat src/agent_session_detective/ir/analyses.py tests/test_ir_analyses.py
git add src/agent_session_detective/ir/analyses.py tests/test_ir_analyses.py
git commit -m "feat: add the subagent-returns bloat ledger to IR analyses"
```

---

### Task 2: `bill.py` pure builder

**Files:**
- Create: `src/agent_session_detective/bill.py`
- Create: `tests/test_bill.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bill.py`:

```python
"""Bill summary (P0-1) — pure-function tests over dict fixtures.

The honesty rules under test: shares only within the EST subtotal,
no volume → row omitted (never zero-filled), None means no evidence,
billed totals kept out of EST math.
"""

import unittest

from agent_session_detective.bill import build_bill


def ts(**overrides):
    base = {
        "cache_hit_rate": 0.5,
        "input_total": 1000,
        "output_total": 500,
        "cache_read_total": 500,
        "growth_verdict": "linear",
        "turn_growth": [],
        "bucket_totals": {"system": 1000, "history": 0, "inject": 2000,
                          "skill": 3000, "tool": 4000, "output": 0},
        "bucket_shares": {},
        "hash_runs": [],
        "hash_flips": None,
        "repeats": [],
        "repeat_extra_tokens": 1000,
        "repeat_class_totals": {"post_compaction": 400},
        "compaction_source": "transcript",
        "compaction_points": [{"ts": "t1"}, {"ts": "t2"}],
        "usage_record_count": 3,
    }
    base.update(overrides)
    return base


LOADS = {"rows": [], "totals": {"loads": 2, "reloads": 1,
                                "unavailable": 1, "redundant_bodies": 0,
                                "cost_tokens_est": 500}, "channels": {}}


def returns(**overrides):
    base = {"rows": [], "totals": {"dispatches": 2, "linked": 1,
                                   "return_tokens_total": 3000,
                                   "flagged": 1,
                                   "process_unavailable": 1}}
    base.update(overrides)
    return base


def bill(**overrides):
    params = {
        "token_stats": ts(),
        "skill_loads": LOADS,
        "returns": returns(),
        "expectations": [],
        "judgments": {"missed": [], "errors": []},
        "if_results": [],
        "billed_series": [],
        "file_read_count": 0,
        "adapter_id": "qoder",
        "judge_enabled": False,
    }
    params.update(overrides)
    return build_bill(**params)


class BuildBillTest(unittest.TestCase):
    def test_all_six_rows_sorted_by_est_volume_with_shares(self):
        out = bill()
        self.assertEqual([row["key"] for row in out["cost_rows"]],
                         ["context_growth", "subagent_returns",
                          "repeat_tax", "skill_load_cost", "output",
                          "cache_signal"])
        by_key = {row["key"]: row for row in out["cost_rows"]}
        # four round(..., 4) shares of 14500 drift up to ~1.0001 in total
        self.assertAlmostEqual(
            sum(row["share"] for row in out["cost_rows"]
                if row["share"] is not None), 1.0, delta=0.0003)
        self.assertEqual(out["est_subtotal"], 14500)
        self.assertEqual(by_key["context_growth"]["tokens_est"], 10000)
        self.assertEqual(by_key["context_growth"]["share"],
                         round(10000 / 14500, 4))
        self.assertEqual(by_key["context_growth"]["grade"], "推断")
        self.assertIn("attribution 90%", by_key["context_growth"]["detail"])
        self.assertIn("压缩 2 次(transcript)",
                      by_key["context_growth"]["detail"])
        self.assertEqual(by_key["cache_signal"]["grade"], "观测")
        self.assertIn("50%", by_key["cache_signal"]["detail"])
        self.assertIsNone(out["billed"])
        self.assertEqual(out["notes"], [])

    def test_rows_without_evidence_keep_null_numbers(self):
        out = bill(token_stats=ts(cache_hit_rate=None, usage_record_count=0,
                                  compaction_points=[],
                                  compaction_source=None))
        by_key = {row["key"]: row for row in out["cost_rows"]}
        self.assertIsNone(by_key["output"]["tokens_est"])
        self.assertIsNone(by_key["output"]["share"])
        self.assertIn("unavailable", by_key["output"]["detail"])
        self.assertIn("无缓存信号", by_key["cache_signal"]["detail"])
        self.assertIn("无证据源", by_key["context_growth"]["detail"])

    def test_residual_heavy_growth_and_billed_block(self):
        out = bill(
            token_stats=ts(
                cache_hit_rate=None,
                bucket_totals={"system": 9000, "history": 0, "inject": 1000,
                               "skill": 0, "tool": 0, "output": 0}),
            skill_loads={"rows": [], "totals": {
                "loads": 1, "reloads": 0, "unavailable": 1,
                "redundant_bodies": 0, "cost_tokens_est": 0},
                "channels": {}},
            billed_series=[
                {"ts": "t1", "prompt": 100, "completion": 20, "cached": 60},
                {"ts": "t2", "prompt": 200, "completion": 30, "cached": 100},
            ])
        by_key = {row["key"]: row for row in out["cost_rows"]}
        self.assertIn("attribution 10%", by_key["context_growth"]["detail"])
        self.assertIn("残差主导", by_key["context_growth"]["detail"])
        self.assertIsNone(by_key["skill_load_cost"]["tokens_est"])
        self.assertIn("1 unavailable", by_key["skill_load_cost"]["detail"])
        self.assertEqual(out["est_subtotal"], 14000)
        self.assertEqual(out["billed"]["requests"], 2)
        self.assertEqual(out["billed"]["prompt_total"], 300)
        self.assertEqual(out["billed"]["cached_share"], 0.5333)
        self.assertIn("账单口径", by_key["cache_signal"]["detail"])

    def test_qoder_without_billed_series_gets_the_howto_note(self):
        out = bill(billed_series=[])
        self.assertEqual(len(out["cost_rows"]), 6)
        self.assertEqual(out["notes"], [
            "账单通道未接入：勾选 billed usage（web）/ "
            "--billed-usage（CLI）后重跑可接入 Qoder 账单；"
            "已勾选仍见此提示，则是账单库本次不可用"])

    def test_non_qoder_adapter_gets_no_billed_note(self):
        out = bill(adapter_id="kimi-cli")
        self.assertEqual(out["notes"], [])

    def test_no_dispatches_omits_the_subagent_row(self):
        out = bill(returns=returns(rows=[], totals={
            "dispatches": 0, "linked": 0, "return_tokens_total": 0,
            "flagged": 0, "process_unavailable": 0}))
        self.assertEqual([row["key"] for row in out["cost_rows"]],
                         ["context_growth", "repeat_tax",
                          "skill_load_cost", "output", "cache_signal"])
        self.assertEqual(out["est_subtotal"], 11500)


EXPECT = [
    {"name": "a", "status": "loaded"},
    {"name": "b", "status": "missing"},
    {"name": "c", "status": "missing"},
    {"name": "d", "status": "file-read"},
]

IFS = [
    {"playbook": "p1", "coverage": 0.5, "gate": 0.8, "passed": False,
     "not_applicable": False, "verdicts": []},
    {"playbook": "p2", "coverage": 1.0, "gate": 0.8, "passed": True,
     "not_applicable": True, "verdicts": []},
]

JUDGMENTS = {"missed": [{"skill_name": "ghost"}], "errors": []}


class RoutingTest(unittest.TestCase):
    def test_judge_on_renders_all_four_lines(self):
        routing = bill(expectations=EXPECT, judgments=JUDGMENTS,
                       if_results=IFS, file_read_count=4,
                       judge_enabled=True)["routing"]
        self.assertEqual(
            [(l["label"], l["value"], l["grade"], l["anchor"])
             for l in routing["lines"]],
            [("expect 缺失", "2/4", "判断", "#expectations"),
             ("judge 命中", "1", "判断", "#findings"),
             ("IF 覆盖", "0.50", "判断", "#if"),
             ("SKILL.md 直读", "4 次", "事实", "#loads")])
        self.assertTrue(routing["enabled"])

    def test_judge_off_keeps_fact_lines_only(self):
        routing = bill(expectations=EXPECT, file_read_count=4,
                       judge_enabled=False)["routing"]
        self.assertEqual([l["label"] for l in routing["lines"]],
                         ["expect 缺失", "SKILL.md 直读"])
        self.assertFalse(routing["enabled"])

    def test_no_signals_renders_no_lines(self):
        routing = bill()["routing"]
        self.assertEqual(routing["lines"], [])
        self.assertFalse(routing["enabled"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_bill.py -q`
Expected: **FAIL** — `ModuleNotFoundError: No module named 'agent_session_detective.bill'`.

- [ ] **Step 3: Implement `bill.py`**

Create `src/agent_session_detective/bill.py`:

```python
"""The bill summary (P0-1): what did this session cost, and did the
right things load.

A pure function over already-serialized blocks — ``token_stats``,
``skill_loads``, ``subagent_returns``, the expectation/judgment/IF
summaries, and the optional billed series. Imports nothing from
``ir/`` or ``billing``; the web layer owns every join.

Honesty rules (spec):
- EST/observed numbers are never divided by billed denominators;
  shares are computed only within the EST subtotal.
- A row with no evidence keeps ``tokens_est: None`` — never
  zero-filled; the UI renders the unavailable marker.
- The billed block is session-scoped and never mixed into EST math.
"""

from __future__ import annotations

EST_ROW_KEYS = ("context_growth", "repeat_tax", "skill_load_cost",
                "subagent_returns")

GRADE_FACT = "事实"
GRADE_EVIDENCE = "证据"
GRADE_EST = "推断"
GRADE_OBSERVED = "观测"
GRADE_JUDGMENT = "判断"


def _round_share(part, whole):
    if part is None or not whole:
        return None
    return round(part / whole, 4)


def _pct(rate):
    if rate is None:
        return "unknown"
    return "%d%%" % round(rate * 100)


def _billed_block(billed_series):
    if not billed_series:
        return None
    prompt_total = sum(r.get("prompt") or 0 for r in billed_series)
    completion_total = sum(r.get("completion") or 0 for r in billed_series)
    cached_total = sum(r.get("cached") or 0 for r in billed_series)
    return {
        "requests": len(billed_series),
        "prompt_total": prompt_total,
        "completion_total": completion_total,
        "cached_total": cached_total,
        "cached_share": _round_share(cached_total, prompt_total),
        "note": "会话级账单口径（SharedClientCache），不与日志侧 EST 混算",
    }


def _routing(expectations, judgments, if_results, file_read_count,
             judge_enabled):
    lines = []
    if expectations:
        missing = sum(1 for e in expectations
                      if e.get("status") == "missing")
        lines.append({"label": "expect 缺失",
                      "value": "%d/%d" % (missing, len(expectations)),
                      "grade": GRADE_JUDGMENT,
                      "anchor": "#expectations"})
    if judge_enabled:
        missed = len((judgments or {}).get("missed") or [])
        lines.append({"label": "judge 命中", "value": str(missed),
                      "grade": GRADE_JUDGMENT, "anchor": "#findings"})
    applicable = [r for r in (if_results or [])
                  if not r.get("not_applicable")]
    if applicable:
        coverage = (sum(r.get("coverage") or 0.0 for r in applicable)
                    / len(applicable))
        lines.append({"label": "IF 覆盖", "value": "%.2f" % coverage,
                      "grade": GRADE_JUDGMENT, "anchor": "#if"})
    if file_read_count:
        lines.append({"label": "SKILL.md 直读",
                      "value": "%d 次" % file_read_count,
                      "grade": GRADE_FACT, "anchor": "#loads"})
    return {"lines": lines, "enabled": judge_enabled}


def _cost_rows(token_stats, skill_loads_block, returns_block,
               billed_series):
    rows = []
    buckets = token_stats.get("bucket_totals") or {}
    growth = sum(buckets.values())
    if growth:
        residual = buckets.get("system") or 0
        attributed = growth - residual
        detail = "attribution %s · 压缩 %s" % (
            _pct(_round_share(attributed, growth)),
            "%d 次(%s)" % (len(token_stats.get("compaction_points") or []),
                           token_stats.get("compaction_source"))
            if token_stats.get("compaction_source") else "无证据源")
        if residual * 2 > growth:
            detail += " · 残差主导：技能直读类成本可能藏在 system 残差里"
        rows.append({"key": "context_growth", "label": "上下文增长",
                     "tokens_est": growth, "share": None,
                     "detail": detail, "anchor": "#tokens",
                     "grade": GRADE_EST})
    extra = token_stats.get("repeat_extra_tokens") or 0
    if extra:
        post = ((token_stats.get("repeat_class_totals") or {})
                .get("post_compaction") or 0)
        rows.append({"key": "repeat_tax", "label": "重复注入税",
                     "tokens_est": extra, "share": None,
                     "detail": "压缩恢复 ~%d · 其余 ~%d"
                               "（extra 总量是重读税下界）"
                               % (post, max(0, extra - post)),
                     "anchor": "#tokens", "grade": GRADE_EST})
    load_totals = (skill_loads_block or {}).get("totals") or {}
    if load_totals.get("loads"):
        cost = load_totals.get("cost_tokens_est") or 0
        rows.append({"key": "skill_load_cost", "label": "技能加载成本",
                     "tokens_est": cost or None, "share": None,
                     "detail": "%d loads · %d unavailable · %d reloads"
                               % (load_totals.get("loads"),
                                  load_totals.get("unavailable"),
                                  load_totals.get("reloads")),
                     "anchor": "#loads", "grade": GRADE_EVIDENCE})
    return_totals = (returns_block or {}).get("totals") or {}
    if return_totals.get("dispatches"):
        total = return_totals.get("return_tokens_total") or 0
        rows.append({"key": "subagent_returns", "label": "subagent 返回",
                     "tokens_est": total or None, "share": None,
                     "detail": "%d 次派发 · %d 次臃肿（判定）"
                               " · %d 次过程量不可用"
                               % (return_totals.get("dispatches"),
                                  return_totals.get("flagged"),
                                  return_totals.get(
                                      "process_unavailable")),
                     "anchor": "#tokens", "grade": GRADE_FACT})
    if token_stats.get("usage_record_count"):
        output_tokens = token_stats.get("output_total")
        output_detail = "usage 遥测（%d 条记录）" % (
            token_stats["usage_record_count"])
    else:
        output_tokens = None
        output_detail = "无输出遥测，unavailable"
    rows.append({"key": "output", "label": "输出",
                 "tokens_est": output_tokens, "share": None,
                 "detail": output_detail, "anchor": "#tokens",
                 "grade": GRADE_OBSERVED})
    billed = _billed_block(billed_series)
    if token_stats.get("cache_hit_rate") is not None:
        cache_detail = "cache 命中 %s（日志 usage 口径）" % _pct(
            token_stats["cache_hit_rate"])
    elif billed is not None:
        cache_detail = "cache 命中 %s（账单口径）" % _pct(
            billed["cached_share"])
    else:
        cache_detail = "无缓存信号"
    rows.append({"key": "cache_signal", "label": "缓存信号",
                 "tokens_est": None, "share": None,
                 "detail": cache_detail, "anchor": "#tokens",
                 "grade": GRADE_OBSERVED})
    return rows


def build_bill(token_stats, skill_loads, returns, expectations, judgments,
               if_results, billed_series, file_read_count, adapter_id,
               judge_enabled):
    """The bill: cost rows + routing lines + billed block + notes."""
    rows = _cost_rows(token_stats, skill_loads, returns, billed_series)
    by_key = {row["key"]: row for row in rows}
    est_subtotal = sum(by_key[key]["tokens_est"] or 0
                       for key in EST_ROW_KEYS if key in by_key)
    for key in EST_ROW_KEYS:
        row = by_key.get(key)
        if row is None or row["tokens_est"] is None:
            continue
        row["share"] = _round_share(row["tokens_est"], est_subtotal)
    volume = [by_key[key] for key in EST_ROW_KEYS if key in by_key]
    volume.sort(key=lambda row: -(row["tokens_est"] or 0))
    ordered = volume + [row for row in rows
                        if row["key"] not in EST_ROW_KEYS]
    notes = []
    if not billed_series and (adapter_id or "").startswith("qoder"):
        notes.append(
            "账单通道未接入：勾选 billed usage（web）/ "
            "--billed-usage（CLI）后重跑可接入 Qoder 账单；"
            "已勾选仍见此提示，则是账单库本次不可用")
    return {
        "cost_rows": ordered,
        "est_subtotal": est_subtotal,
        "routing": _routing(expectations, judgments, if_results,
                            file_read_count, judge_enabled),
        "billed": _billed_block(billed_series),
        "notes": notes,
    }
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_bill.py -q`
Expected: **PASS** (9 tests).

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: **493 passed** (484 + 9).

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/bill.py tests/test_bill.py
git commit -m "feat: add the bill summary builder"
```

---

### Task 3: Wire the bill into `run_audit` + fingerprint v11

**Files:**
- Modify: `src/agent_session_detective/web.py` (top import, run_audit locals, payload dict, fingerprint)
- Test: `tests/test_web.py` (rename 2 fingerprint tests, add RunAuditBillTests)

⚠️ **Shared-file guard:** `web.py` and `tests/test_web.py` are dirty with the concurrent session's sidebar-search WIP. Before the commit in Step 7, run `git diff src/agent_session_detective/web.py tests/test_web.py` and confirm every hunk is bill-related. If sidebar-search hunks are present, STOP and ask the user.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, rename and rewrite the two v10 fingerprint tests. Replace:

```python
    def test_fingerprint_uses_v10_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v10:ir%s" % IR_VERSION,
            )

    def test_v10_fingerprint_invalidates_v9_cached_results(self):
        # v9 cached billed results lack the turn-growth series rebuilt from
        # the billed prompt rows; the v10 bump must make cache_load miss.
        v9_fp = "1234.000:5:test-model:v9:ir1.6"
        v10_fp = "1234.000:5:test-model:v10:ir1.6"
        result = {"status_line": "stale v9 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v9_fp, result)
            self.assertEqual(cache_load("k", v9_fp), result)
            self.assertIsNone(cache_load("k", v10_fp))
        finally:
            web.CACHE_DIR = original_cache_dir
```

with:

```python
    def test_fingerprint_uses_v11_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v11:ir%s" % IR_VERSION,
            )

    def test_v11_fingerprint_invalidates_v10_cached_results(self):
        # v10 cached results lack the bill + subagent_returns blocks; the
        # v11 bump must make cache_load miss.
        v10_fp = "1234.000:5:test-model:v10:ir1.6"
        v11_fp = "1234.000:5:test-model:v11:ir1.6"
        result = {"status_line": "stale v10 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v10_fp, result)
            self.assertEqual(cache_load("k", v10_fp), result)
            self.assertIsNone(cache_load("k", v11_fp))
        finally:
            web.CACHE_DIR = original_cache_dir
```

Then, **before** the trailing `if __name__ == "__main__":` block, add:

```python
class RunAuditBillTests(unittest.TestCase):
    def test_run_audit_payload_carries_the_bill_and_returns_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / (
                "3b241101-e2bb-4255-8caf-4136c566a962.jsonl")
            transcript.write_text(
                '{"type": "context.append_loop_event", "event": '
                '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
                '"args": {"path": "a"}}}\n'
                '{"type": "context.append_loop_event", "event": '
                '{"type": "tool.result", "toolCallId": "c1", "result": '
                '{"output": "hello world"}}}\n',
                encoding="utf-8")

            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            bill = job.result["bill"]
            self.assertEqual(sorted(bill),
                             ["billed", "cost_rows", "est_subtotal",
                              "notes", "routing"])
            keys = [row["key"] for row in bill["cost_rows"]]
            self.assertIn("context_growth", keys)
            self.assertIn("output", keys)
            self.assertEqual(keys[-1], "cache_signal")
            self.assertEqual(job.result["subagent_returns"]["rows"], [])
            self.assertTrue(
                any("账单通道未接入" in note for note in bill["notes"]))


```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web.py -q -k "fingerprint or RunAuditBill"`
Expected: **FAIL** — the v11 tests fail against the still-v10 fingerprint; `RunAuditBillTests` fails on `job.status` ("error" != "done" — run_audit's except clause captures the missing-`bill` KeyError into `job.error`).

- [ ] **Step 3: Wire run_audit**

In `src/agent_session_detective/web.py`, add the top-level import directly above `from .catalog import load_catalog`:

```python
from .bill import build_bill
```

Inside `run_audit`, extend the local import block. Replace:

```python
        from .ir.analyses import skill_loads as skill_loads_rollup
        from .ir.builder import build_audit_document
```

with:

```python
        from .ir.analyses import skill_loads as skill_loads_rollup
        from .ir.analyses import subagent_returns as subagent_returns_rollup
        from .ir.builder import build_audit_document
```

Replace:

```python
        missed = [f["skill_name"] for f in judgments_to_dict(judgments)["missed"]]
        load_block = skill_loads_rollup(document)
        load_totals = load_block["totals"]
```

with:

```python
        judgments_block = judgments_to_dict(judgments)
        missed = [f["skill_name"] for f in judgments_block["missed"]]
        load_block = skill_loads_rollup(document)
        returns_block = subagent_returns_rollup(document)
        load_totals = load_block["totals"]
        token_stats_block = tokenstats_to_dict(token_stats)
        if_block = if_results_to_dict(if_results)
        bill = build_bill(
            token_stats_block, load_block, returns_block, expectations,
            judgments_block, if_block, billed_series,
            len(timeline.file_reads), adapter_id, judge_enabled)
```

In the `job.result` payload, replace:

```python
            "timeline": timeline_to_dict(timeline),
            "judgments": judgments_to_dict(judgments),
            "if_results": if_results_to_dict(if_results),
            "expectations": expectations,
            "event_feed": event_feed_to_list(session),
            "token_stats": tokenstats_to_dict(token_stats),
            "skill_loads": load_block,
```

with:

```python
            "timeline": timeline_to_dict(timeline),
            "judgments": judgments_block,
            "if_results": if_block,
            "expectations": expectations,
            "event_feed": event_feed_to_list(session),
            "token_stats": token_stats_block,
            "skill_loads": load_block,
            "bill": bill,
            "subagent_returns": returns_block,
```

(The `suggestions` call keeps passing the original objects — `timeline, expectations, judgments, if_results, judge_enabled, token_stats` — unchanged.)

- [ ] **Step 4: Bump the fingerprint to v11**

In `fingerprint()`, replace:

```python
    # v10 feeds the billed prompt series into turn_growth: v9 cached results
    # for billed CLI-shape logs (no transcript telemetry) lack the growth
    # rows and would keep rendering the "no context telemetry" gap note.
    return "%.3f:%d:%s:v10:ir%s" % (newest, total, judge_model, IR_VERSION)
```

with:

```python
    # v10 feeds the billed prompt series into turn_growth: v9 cached results
    # for billed CLI-shape logs (no transcript telemetry) lack the growth
    # rows and would keep rendering the "no context telemetry" gap note.
    # v11 adds the bill + subagent_returns payload blocks (P0-1): v10
    # cached results would render without the bill card.
    return "%.3f:%d:%s:v11:ir%s" % (newest, total, judge_model, IR_VERSION)
```

- [ ] **Step 5: Run the web tests to verify they pass**

Run: `python -m pytest tests/test_web.py -q`
Expected: **PASS** (existing web suite + 1 new test, 2 renamed).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: **494 passed** (493 + 1 new; the 2 renames keep the count flat).

- [ ] **Step 7: Commit (with the shared-file guard)**

```bash
git diff src/agent_session_detective/web.py tests/test_web.py
```

Verify every hunk is bill-related (imports, run_audit blocks, fingerprint v11, the new test class). If sidebar-search hunks appear, STOP and ask the user. Otherwise:

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: wire the bill into the web report payload"
```

---

### Task 4: Render the bill card (webapp)

**Files:**
- Modify: `src/agent_session_detective/webapp/app.js` (HELP table, `renderBill` function, `renderReport` insertion, five section ids)

⚠️ **Shared-file guard:** `app.js` is dirty with the concurrent session's sidebar-search WIP. Same diff guard before the commit in Step 6.

- [ ] **Step 1: Add the HELP entry**

In the `HELP` table, directly after the `"loads"` entry (its text ends with `"这里是全报告唯一出现加载成本数字的地方。"`), add:

```javascript
    ,
    "bill":
      "双账单：成本行回答 tokens 废在哪，路由行回答该加载的加载了吗。" +
      "行等级：事实 = 渠道直接测量；观测 = provider 遥测；证据 = 配对台账；推断 = 估算。" +
      "share 只在 EST 口径小计内计算（上下文增长/重复注入税/技能加载成本/subagent 返回），" +
      "输出与缓存率不入分母；无分母则不显示百分比。subagent 行可展开查看臃肿派发明细。"
```

(The concurrent session may have added other HELP keys after `"loads"` — if so, insert this entry before the closing `};` of the HELP object instead; the entry order does not matter.)

- [ ] **Step 2: Add `renderBill` and call it first in `renderReport`**

Replace:

```javascript
  function renderReport(r) {
    var parts = [];
```

with:

```javascript
  function renderBill(bill, returns) {
    var GRADE_CLS = {"事实": "fact", "观测": "ok", "证据": "warn",
                     "推断": "dim", "判断": "dim"};
    var rows = bill.cost_rows.map(function (row) {
      var tok = row.tokens_est == null
        ? '<span class="unavailable">unavailable</span>'
        : "~" + row.tokens_est + " (EST)";
      var share = row.share == null ? "—" : (row.share * 100).toFixed(1) + "%";
      var html = "<tr><td>" + esc(row.label) + "</td><td>" + tok +
        "</td><td>" + share + "</td><td>" + esc(row.detail) + "</td><td>" +
        badge(GRADE_CLS[row.grade] || "dim", row.grade) + "</td></tr>";
      if (row.key === "subagent_returns" && returns) {
        (returns.rows || []).filter(function (rr) {
          return (rr.flagged_reasons || []).length;
        }).slice(0, 3).forEach(function (rr) {
          var proc = rr.subagent_tokens_est == null
            ? "unavailable" : "~" + rr.subagent_tokens_est;
          var ret = rr.return_tokens_est == null
            ? "unavailable" : "~" + rr.return_tokens_est;
          html += '<tr class="bill-sub-row"><td colspan="5">' +
            badge("warn", "bloat") + " " + esc(rr.dispatch_id) +
            " · 返回 " + ret + " · 过程 " + proc +
            (rr.ratio == null ? "" : " · 比 " + rr.ratio) +
            " · " + esc((rr.flagged_reasons || []).join(" + ")) +
            (rr.description ? " · " + esc(rr.description) : "") +
            "</td></tr>";
        });
      }
      return html;
    }).join("");
    var h = '<section id="bill"><h2>bill · 成本与路由' + helpDot("bill") + "</h2>" +
      '<table class="bill-table"><thead><tr><th>成本项</th><th>tokens</th>' +
      "<th>share</th><th>detail</th><th>等级</th></tr></thead><tbody>" +
      rows +
      '<tr><td class="dim">EST 小计</td><td>~' + bill.est_subtotal +
      '</td><td colspan="3" class="dim">share 分母，不含 output / 缓存率</td></tr>' +
      "</tbody></table>";
    var routing = bill.routing || {lines: [], enabled: false};
    if (routing.lines.length) {
      h += '<div class="bill-routing">' +
        routing.lines.map(function (l) {
          return '<a href="' + esc(l.anchor) + '">' + esc(l.label) + " " +
            esc(l.value) + " " + badge("dim", l.grade) + "</a>";
        }).join("") +
        (routing.enabled ? ""
         : '<span class="dim">judge 未启用，路由账单不完整。</span>') +
        "</div>";
    } else {
      h += '<p class="dim">无路由信号。</p>';
    }
    if (bill.billed) {
      var cachePct = bill.billed.cached_share == null
        ? "unknown" : Math.round(bill.billed.cached_share * 100) + "%";
      h += '<p class="dim">billed（会话级，' + bill.billed.requests +
        " 请求 · prompt " + bill.billed.prompt_total +
        " · completion " + bill.billed.completion_total +
        " · cache " + cachePct + "）：" + esc(bill.billed.note) + "</p>";
    }
    (bill.notes || []).forEach(function (n) {
      h += '<p class="dim">' + esc(n) + "</p>";
    });
    return h + "</section>";
  }

  function renderReport(r) {
    var parts = [];
    if (r.bill) {
      parts.push(renderBill(r.bill, r.subagent_returns));
    }
```

(The `if (r.bill)` guard keeps stale cached payloads — pre-v11 fingerprints — rendering gracefully without the card.)

- [ ] **Step 3: Add the five section ids (anchor targets)**

Five content-anchored edits, one per section header:

1. `return "<section><h2>token governance " + helpDot("tokens") + "</h2>" + parts.join("") + "</section>";`
   → `return "<section id='tokens'><h2>token governance " + helpDot("tokens") + "</h2>" + parts.join("") + "</section>";`
2. `parts.push("<section><h2>expectations " + helpDot("expect") + "</h2><table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");`
   → `parts.push("<section id='expectations'><h2>expectations " + helpDot("expect") + "</h2><table><tr><th>skill</th><th>status</th></tr>" + rows + "</table></section>");`
3. `parts.push("<section><h2>findings " + helpDot("findings") + "</h2>" + body + "</section>");`
   → `parts.push("<section id='findings'><h2>findings " + helpDot("findings") + "</h2>" + body + "</section>");`
4. `"<section><h2>instruction following · " + esc(res.playbook) + " " + helpDot("if") + "</h2>" +`
   → `"<section id='if'><h2>instruction following · " + esc(res.playbook) + " " + helpDot("if") + "</h2>" +`
5. `"<section><h2>skill loads (evidence-backed) " + helpDot("loads") + "</h2>" +`
   → `"<section id='loads'><h2>skill loads (evidence-backed) " + helpDot("loads") + "</h2>" +`

Known caveat: with multiple playbooks every applicable IF section carries `id='if'` (duplicate DOM ids). The anchor jumps to the first one, which is the intended target. The n/a IF section stays without an id.

- [ ] **Step 4: Syntax-check**

Run: `node --check src/agent_session_detective/webapp/app.js`
Expected: no output, exit 0.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: **494 passed** (no Python change in this task).

- [ ] **Step 6: Commit (with the shared-file guard)**

```bash
git diff src/agent_session_detective/webapp/app.js
```

Verify every hunk is bill-related. If sidebar-search hunks appear, STOP and ask the user. Otherwise:

```bash
git add src/agent_session_detective/webapp/app.js
git commit -m "feat: render the bill card in the webapp"
```

---

### Task 5: Bill card styles

**Files:**
- Modify: `src/agent_session_detective/webapp/style.css` (append at end of file)

⚠️ **Shared-file guard:** `style.css` is dirty with the concurrent session's WIP. Same diff guard before the commit in Step 3.

- [ ] **Step 1: Append the styles**

At the end of `src/agent_session_detective/webapp/style.css` (after the feed-filter styles and the `.hidden` rule), append:

```css
/* bill card (P0-1): 5-column cost table + routing line chips */
.bill-table td {
  padding: 4px 8px 4px 0;
  vertical-align: top;
}
.bill-table td:nth-child(2) {
  text-align: right;
  white-space: nowrap;
}
.bill-sub-row td {
  padding: 0 0 6px 24px;
  font-size: 12px;
  color: var(--dim);
}
.bill-routing {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12.5px;
}
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: **494 passed** (no Python change).

- [ ] **Step 3: Commit (with the shared-file guard)**

```bash
git diff src/agent_session_detective/webapp/style.css
```

Verify every hunk is bill-related. Otherwise STOP and ask. Then:

```bash
git add src/agent_session_detective/webapp/style.css
git commit -m "feat: style the bill card"
```

---

### Task 6: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite one last time**

Run: `python -m pytest tests/ -q`
Expected: **494 passed**.

- [ ] **Step 2: Serve on the isolated port**

Run: `python -m agent_session_detective.cli --serve --port 8472` (background)
Expected: server starts on 8472. **Never touch 8471** — that instance belongs to another session.

- [ ] **Step 3: Browser golden path**

Open `http://127.0.0.1:8472/`, load any discoverable session (or POST a path with `billed` unchecked first). Verify:

1. The bill card renders ABOVE the summary section, with the `bill · 成本与路由` heading and a working `?` help popover.
2. Six cost rows for a session with volume; the unavailable marker (not `0`) where there is no evidence.
3. The EST 小计 row shows the share denominator.
4. Each routing line (`expect 缺失`, `judge 命中`, `IF 覆盖`, `SKILL.md 直读`) jumps to its section when clicked (verify at least one).
5. For a session with subagent dispatches flagged bloat: a `bloat` sub-row appears under the `subagent 返回` row.
6. With `billed` unchecked and a qoder adapter: the 账单通道未接入 note renders. With `billed` checked and the billing DB available: the billed paragraph renders instead.
7. The judge-off case shows the `judge 未启用，路由账单不完整。` hint.
8. No JS console errors (`list_console_messages` should be clean or contain only pre-existing messages).

- [ ] **Step 4: Stop the 8472 server**

Stop only the process started in Step 2 (by the PID you captured), never anything on 8471.

- [ ] **Step 5: Final commit if anything is left**

Run: `git status --porcelain`
Expected: only the concurrent session's original dirty files remain (README.md, index.html — files this plan never edits). If any bill-related file is still unstaged, stage it by exact path and commit `feat: finish the bill summary wiring`.

---

## Self-Review Checklist (for the executor's final pass)

- Spec coverage: dual bill (cost rows + routing) ✓ Task 2; fixed six rows with grades ✓ Task 2 `_cost_rows`; share only within EST subtotal ✓ Task 2 `build_bill`; routing anchors ✓ Task 4 Step 3; subagent return bloat (2000 + echo) ✓ Task 1; payload keys `bill` + `subagent_returns` ✓ Task 3; fingerprint v11, cache_key unchanged ✓ Task 3; web-only (no CLI surface) ✓; existing tests untouched except the two v10→v11 renames ✓.
- Honesty: no EST/billed division ✓ (`est_subtotal` only sums EST rows); no volume → row omitted ✓ (`if growth:`, `if extra:`, `if load_totals.get("loads"):`, `if return_totals.get("dispatches"):`); billed block separate ✓.
- The `suggestions` call in `run_audit` still receives objects (not the serialized blocks) — verify with `git diff` after Task 3.
