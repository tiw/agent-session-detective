# 流程签名（直通 vs pipeline）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「直通 vs pipeline」变成可测量观测对比——每会话流程签名（style 三分类 + 机制特征）、ceremony 透镜账单行（listing 重注入 + 协议重读）、fleet 按 style 分组聚合。

**Architecture:** 新纯函数模块 `ir/process.py` 只读 IR document（items/skills/skill_loads/interventions），注册进 `build_analyses`；前置 IR 1.9 给 `ContentItem` 加 `file_path`（tool_call 参数在 extraction 时解析，全参数文本只在那里可得）；ceremony 行挂进 `build_bill`（display-only，不入 EST 小计，避免与 context_growth/repeat_tax 重复计费）；fleet_stats 每会话建 IR document 聚合 `process_groups`。

**Tech Stack:** Python 3 stdlib + unittest（测试命令 `python -m unittest discover -s tests`，在仓库根运行；src 布局由 pyproject 可编辑安装解析）；报告为内联 HTML 字符串；web 面板为 `webapp/app.js` 字符串拼接。

**Spec:** `docs/superpowers/specs/2026-10-12-process-signature-design.md`

**关键事实（写死，别再探查）:**
- 指纹格式 `web.py:139` `"%.3f:%d:%s:v12:ir%s"` —— IR 1.8→1.9 使 `ir%s` 段自变，旧缓存自失效，**不需要**手工 v13（payload 新增 `process` 键与 ceremony 行全部由 IR/session 派生）。
- `IR_VERSION` pin 断言在 `tests/test_ir_schema.py:124` 与 `tests/test_ir_dispatch.py:462`（当前都是 `"1.8"`）。
- `tests/test_bill.py:69` 钉死了六行顺序——ceremony 行必须只在传 `process` 且数值 >0 时出现，否则现有测试全红。
- qoder 合成 transcript 的记录形状直接抄 `tests/test_ir_items.py` 的 helper（user/assistant/tool_result/attachment）；fleet 发现路径 `web.py:500` `base.glob("*/*.jsonl")`，测试布局 `<tmp>/ws/<uuid>.jsonl`。
- `render_report` 当前签名在 `report.py:294-307`（末参 `gap`）；Skill GAP 节止于 `report.py:515` 附近，其后是 `# --- Instruction Following ---`。
- app.js：HELP 字典 `"gaps"` 键后（93 行前）加 `"process"`；单会话面板插在 `parts.push(renderTokenStats(r.token_stats));`（1433 行）之前；`renderFleet` 在 304-326 行。

---

### Task 1: IR 1.9 — ContentItem.file_path

**Files:**
- Modify: `src/agent_session_detective/ir/schema.py:15-20`（版本注释 + IR_VERSION）、`ContentItem`（`human_text` 字段后加 `file_path`）
- Modify: `src/agent_session_detective/ir/items.py`（ToolCall 分支 + `add()` 签名）
- Test: `tests/test_ir_items.py`（追加）、`tests/test_ir_schema.py:124`、`tests/test_ir_dispatch.py:462`

- [ ] **Step 1: 写失败测试**

在 `tests/test_ir_items.py` 末尾追加（文件已有 `assistant`/`USAGE` helper 与 `ItemsTest` 基类）：

```python
class ToolCallFilePathTest(IREventsTestCase):
    def extract(self, lines, agent_id="main"):
        records = group_records(self.load_lines(lines).events)
        return extract_items(records, agent_id)

    def test_read_write_calls_carry_file_path(self):
        extraction = self.extract(
            [
                assistant(1, [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/ws/.changes/demo/progress.md"}},
                    {"type": "tool_use", "id": "t2", "name": "Write",
                     "input": {"file_path": "/ws/README.md"}},
                ], usage=USAGE),
            ]
        )
        self.assertEqual(
            [i.file_path for i in extraction.items],
            ["/ws/.changes/demo/progress.md", "/ws/README.md"])

    def test_call_without_path_argument_stays_none(self):
        extraction = self.extract(
            [assistant(1, [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "ls"}},
            ], usage=USAGE)])
        self.assertIsNone(extraction.items[0].file_path)

    def test_skill_call_has_no_file_path(self):
        extraction = self.extract(
            [assistant(1, [
                {"type": "tool_use", "id": "t1", "name": "Skill",
                 "input": {"skill": "demo"}},
            ], usage=USAGE)])
        self.assertIsNone(extraction.items[0].file_path)
```

同时把两处 pin 改为 `"1.9"`：`tests/test_ir_schema.py:124`、`tests/test_ir_dispatch.py:462`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_ir_items.ToolCallFilePathTest tests.test_ir_schema tests.test_ir_dispatch -v`
Expected: FAIL — `AttributeError: 'ContentItem' object has no attribute 'file_path'`；两处 pin 断言 FAIL（"1.8" != "1.9"）

- [ ] **Step 3: 实现 schema.py**

3a. 版本块（15-20 行）替换为：

```python
# IR 1.9: ContentItem.file_path — tool-call arguments are parsed at
# extraction time, the only place the full argument text exists (items keep
# a preview). Consumers: process-signature workflow-file matching; a full
# file-read ledger can reuse the field later. Cached web results must
# recompute (fingerprint embeds ir<IR_VERSION>).
IR_VERSION = "1.9"
```

3b. `ContentItem` 在 `human_text` 字段定义之后追加：

```python
    # File-path argument (IR 1.9): parsed from this tool call's arguments
    # at extraction; None = no path key or unparseable arguments.
    file_path: Optional[str] = None
```

- [ ] **Step 4: 实现 items.py**

4a. 模块顶部 helper（`skill_signature` 之前加）：

```python
def _parse_tool_arguments(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
```

4b. `add()` 内部函数签名加 `file_path=None` 形参，`ContentItem(...)` 构造加 `file_path=file_path`：

```python
    def add(record, bucket, kind, text, channel, skill_id=None, name=None,
            by_signature=False, tool_use_id=None, human_text=None,
            file_path=None):
```

（`ContentItem(...)` 里放在 `human_text=human_text,` 之后加 `file_path=file_path,`。）

4c. ToolCall 分支（`elif event.type == "ToolCall":` 整块）替换为：

```python
            elif event.type == "ToolCall":
                function = event.payload.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or ""
                parsed = _parse_tool_arguments(arguments)
                skill_id = None
                lowered = name.lower()
                if lowered == "skill":
                    target = parsed.get("skill")
                    if isinstance(target, str) and target:
                        skill_id = target.lower()
                file_path = parsed.get("file_path")
                if not isinstance(file_path, str) or not file_path:
                    file_path = None
                if lowered == "read" and file_path \
                        and SKILL_FILE_RE.search(file_path):
                    tool_call_id = event.payload.get("id")
                    if tool_call_id is not None:
                        pending_case_reads[tool_call_id] = file_path
                add(record, "assistant", "tool_call", arguments, "qoder:assistant",
                    skill_id=skill_id, name=name or None,
                    tool_use_id=event.payload.get("id"), file_path=file_path)
```

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_ir_items -v`
Expected: PASS（含新增 3 tests）

Run: `grep -rn '"1\.8"' src tests`
Expected: 无输出（若有残留 pin 一并更新）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/ir/schema.py src/agent_session_detective/ir/items.py tests/test_ir_items.py tests/test_ir_schema.py tests/test_ir_dispatch.py
git commit -m "feat: IR 1.9 — tool-call file_path lands in ContentItem"
```

---

### Task 2: ir/process.py — process_signature

**Files:**
- Create: `src/agent_session_detective/ir/process.py`
- Modify: `src/agent_session_detective/ir/analyses.py`（import + `build_analyses` 加键）
- Test: `tests/test_ir_process.py`（新建）、`tests/test_ir_analyses.py`（追加）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_ir_process.py`：

```python
"""Process signature tests — hand-built IR state, no transcripts.

process_signature reads projected IR state (items / skills / skill_loads /
interventions), so the style rule, listing re-injection accounting and
workflow-file matching are tested directly at that boundary (the
FakeTimeline precedent in test_judge.py).
"""

import unittest
from types import SimpleNamespace

from agent_session_detective.ir.process import process_signature
from agent_session_detective.ir.schema import (
    ContentItem,
    Intervention,
    LoadEvidence,
    Observation,
    SkillEntity,
)


def item(seq, bucket="user", kind="user_message", file_path=None, name=None,
         tokens=100):
    return ContentItem(
        item_id="main:%d:0" % seq, agent_id="main", bucket=bucket, kind=kind,
        name=name, channel="qoder:%s" % bucket, wire_seq=seq, gone_seq=None,
        size_chars=tokens * 4, tokens_est=tokens,
        sha1="sha-%d" % seq, norm_sha1="nsha-%d" % seq,
        record={"file": "t.jsonl", "seq": seq}, preview="p%d" % seq,
        file_path=file_path)


def call(seq, name, file_path=None):
    return item(seq, bucket="assistant", kind="tool_call", name=name,
                file_path=file_path)


def entity(skill_id, listing_tokens):
    """One listing observation per entry of ``listing_tokens``."""
    observations = [
        Observation(kind="listing", channel="skill_listing", agent_id="main",
                    call_id=None, ts=float(n), tokens_est=tokens,
                    body_sha1=None, item_id="%s:%d" % (skill_id, n))
        for n, tokens in enumerate(listing_tokens, start=1)]
    return SkillEntity(skill_id=skill_id, name=skill_id,
                       observations=observations)


def load(skill_id, kind="load"):
    return LoadEvidence(load_id="%s-1" % skill_id, agent_id="main",
                        skill_id=skill_id, kind=kind, marker_item_id=None,
                        body_item_id=None, body_sha1=None, channel=None,
                        ts=None, cost_tokens_est=None,
                        cost_basis="unavailable")


def intervention(label):
    return Intervention(item_id="main:1:0", label=label, rule="r",
                        evidence="", carriers=[], lead_sha1="x")


def document(items=(), skills=(), skill_loads=(), interventions=()):
    return SimpleNamespace(items=list(items), skills=list(skills),
                           skill_loads=list(skill_loads),
                           interventions=list(interventions))


class StyleTest(unittest.TestCase):
    def test_direct_has_no_machinery(self):
        sig = process_signature(document(
            items=[item(1, bucket="user", kind="user_message")]))
        self.assertEqual(sig["style"], "direct")
        self.assertEqual(sig["workflow"], {"reads": 0, "writes": 0})
        self.assertEqual(sig["skill_loads"], {"loads": 0, "reloads": 0})

    def test_pipeline_needs_workflow_files_and_loads(self):
        sig = process_signature(document(
            items=[call(1, "Read", "/ws/.changes/demo/progress.md"),
                   call(2, "Write", "/ws/.changes/demo/progress.md")],
            skill_loads=[load("alpha"), load("beta", "reload")]))
        self.assertEqual(sig["style"], "pipeline")
        self.assertEqual(sig["workflow"], {"reads": 1, "writes": 1})
        self.assertEqual(sig["skill_loads"], {"loads": 2, "reloads": 1})

    def test_workflow_without_loads_is_mixed(self):
        sig = process_signature(document(
            items=[call(1, "Read", "/ws/progress.md")]))
        self.assertEqual(sig["style"], "mixed")

    def test_loads_without_workflow_files_is_mixed(self):
        sig = process_signature(document(skill_loads=[load("alpha")]))
        self.assertEqual(sig["style"], "mixed")


class WorkflowMatchingTest(unittest.TestCase):
    def test_progress_md_basename_matches_regardless_of_dir(self):
        sig = process_signature(document(items=[
            call(1, "Read", "/a/b/progress.md"),
            call(2, "Read", "progress.md")]))
        self.assertEqual(sig["workflow"]["reads"], 2)

    def test_changes_directory_content_counts(self):
        sig = process_signature(document(items=[
            call(1, "Edit", "/ws/.changes/demo/tmp/notes.md")]))
        self.assertEqual(sig["workflow"], {"reads": 0, "writes": 1})

    def test_lookalike_paths_do_not_match(self):
        sig = process_signature(document(items=[
            call(1, "Read", "/tmp/my_progress.md"),
            call(2, "Read", "/tmp/progress.md.bak"),
            call(3, "Read", "/tmp/.changes-backup/x.md")]))
        self.assertEqual(sig["workflow"]["reads"], 0)

    def test_other_tools_with_paths_do_not_count(self):
        sig = process_signature(document(items=[
            call(1, "Grep", "/ws/.changes/demo/progress.md"),
            call(2, "Read", None)]))
        self.assertEqual(sig["workflow"]["reads"], 0)


class ListingReinjectTest(unittest.TestCase):
    def test_extra_occurrences_are_reinjection(self):
        sig = process_signature(document(skills=[
            entity("alpha", [10, 10, 10]),
            entity("beta", [7])]))
        self.assertEqual(sig["listing"], {
            "skills": 2, "occurrences": 4,
            "reinject_occurrences": 2, "reinject_tokens_est": 20})

    def test_single_listing_never_reinjected(self):
        sig = process_signature(document(skills=[entity("beta", [7])]))
        self.assertEqual(sig["listing"]["reinject_tokens_est"], 0)

    def test_no_listings_note(self):
        sig = process_signature(document(items=[item(1)]))
        self.assertIn("unmeasurable", " ".join(sig["notes"]))


class InjectAndInterventionsTest(unittest.TestCase):
    def test_inject_bucket_volume(self):
        sig = process_signature(document(items=[
            item(1, bucket="inject", kind="reminder", tokens=100),
            item(2, bucket="inject", kind="memory", tokens=50),
            item(3, bucket="user", kind="user_message", tokens=500)]))
        self.assertEqual(sig["inject"], {"items": 2, "tokens_est": 150})

    def test_signal_excludes_noise_only(self):
        sig = process_signature(document(interventions=[
            intervention("noise"), intervention("noise"),
            intervention("correction"), intervention("unclassified")]))
        self.assertEqual(sig["interventions"], {"total": 4, "signal": 2})
```

在 `tests/test_ir_analyses.py` 末尾追加（该文件已有 `build` helper 与 `build_analyses` import）：

```python
class ProcessKeyTest(unittest.TestCase):
    def test_build_analyses_gains_the_process_key(self):
        analyses = build_analyses(build("reinject.jsonl"))
        sig = analyses["process"]
        self.assertIn(sig["style"], ("pipeline", "direct", "mixed"))
        self.assertEqual(
            sorted(sig),
            ["inject", "interventions", "listing", "notes",
             "skill_loads", "style", "workflow"])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_ir_process tests.test_ir_analyses.ProcessKeyTest -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_session_detective.ir.process'`；ProcessKeyTest FAIL（`KeyError: 'process'`）

- [ ] **Step 3: 写实现**

创建 `src/agent_session_detective/ir/process.py`：

```python
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
```

`src/agent_session_detective/ir/analyses.py`：import 区加

```python
from .process import process_signature
```

`build_analyses` 返回字典加一行（`"skill_tree"` 键后）：

```python
        "process": process_signature(document),
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_ir_process tests.test_ir_analyses -v`
Expected: PASS（test_ir_process 13 tests；ProcessKeyTest 1 test）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/process.py src/agent_session_detective/ir/analyses.py tests/test_ir_process.py tests/test_ir_analyses.py
git commit -m "feat: process signature — machinery features + style rule"
```

---

### Task 3: bill — ceremony 透镜行

**Files:**
- Modify: `src/agent_session_detective/bill.py`（`_cost_rows` 与 `build_bill` 签名）
- Test: `tests/test_bill.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/test_bill.py`：`bill()` helper 的 params 字典加默认 `"process": None,`，末尾追加：

```python
class CeremonyRowTests(unittest.TestCase):
    def test_ceremony_row_sums_listing_and_protocol_without_est_share(self):
        process = {"listing": {"reinject_tokens_est": 200}}
        out = bill(process=process)  # ts() base has post_compaction 400
        by_key = {row["key"]: row for row in out["cost_rows"]}
        row = by_key["ceremony"]
        self.assertEqual(row["tokens_est"], 600)
        self.assertIsNone(row["share"])
        self.assertIn("listing 重注入 ~200", row["detail"])
        self.assertIn("协议重读 ~400", row["detail"])
        # display-only lens: ceremony never enters the EST subtotal
        self.assertEqual(out["est_subtotal"], 14500)

    def test_no_process_block_keeps_the_six_rows(self):
        out = bill()
        self.assertNotIn(
            "ceremony", {row["key"] for row in out["cost_rows"]})

    def test_zero_ceremony_omits_row(self):
        process = {"listing": {"reinject_tokens_est": 0}}
        out = bill(process=process, token_stats=ts(repeat_class_totals={}))
        self.assertNotIn(
            "ceremony", {row["key"] for row in out["cost_rows"]})
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_bill.CeremonyRowTests -v`
Expected: FAIL — `KeyError: 'ceremony'`（前两例）；第三例 PASS（无行本来成立，但签名未支持 `process` kwarg 时全部 TypeError）

- [ ] **Step 3: 实现 bill.py**

3a. `_cost_rows` 签名与 repeat_tax 块后插入（`rows.append({"key": "repeat_tax", ...})` 之后、`load_totals = ...` 之前）：

```python
def _cost_rows(token_stats, skill_loads_block, returns_block,
               billed_series, process=None):
```

```python
    listing_extra = ((process or {}).get("listing") or {}) \
        .get("reinject_tokens_est") or 0
    protocol = ((token_stats.get("repeat_class_totals") or {})
                .get("post_compaction") or 0)
    if listing_extra or protocol:
        rows.append({"key": "ceremony", "label": "仪式开销",
                     "tokens_est": listing_extra + protocol, "share": None,
                     "detail": "listing 重注入 ~%d · 协议重读 ~%d"
                               "（透镜行：分别已含于上下文增长与重复注入税，"
                               "不入 EST 小计）" % (listing_extra, protocol),
                     "anchor": "#tokens", "grade": GRADE_EST})
```

3b. `build_bill` 签名末尾加参并下传：

```python
def build_bill(token_stats, skill_loads, returns, expectations, judgments,
               if_results, billed_series, file_read_count, adapter_id,
               judge_enabled, process=None):
    """The bill: cost rows + routing lines + billed block + notes."""
    rows = _cost_rows(token_stats, skill_loads, returns, billed_series,
                      process)
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_bill -v`
Expected: PASS（含 3 个新 tests，原有六行顺序断言不变——`process=None` 不出行）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/bill.py tests/test_bill.py
git commit -m "feat: ceremony lens row in the bill"
```

---

### Task 4: web — run_audit 载 process + fleet process_groups

**Files:**
- Modify: `src/agent_session_detective/web.py`（`run_audit` 940-1060 区、`fleet_stats` 603-661）
- Test: `tests/test_web.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/test_web.py` 末尾追加（沿用 `FleetClassTotalsTests` 的布局先例）：

```python
PIPELINE_LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-10-12T08:00:01.000Z",
     "message": {"content": [{"type": "text", "text": "run the change"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:02.000Z",
     "attachment": {"type": "skill_listing",
                    "content": "- alpha: Use when testing.\n"
                               "- beta: Use when billing.\n"}},
    {"type": "assistant", "uuid": "a1",
     "timestamp": "2026-10-12T08:00:03.000Z",
     "message": {"content": [
         {"type": "tool_use", "id": "t1", "name": "Skill",
          "input": {"skill": "alpha"}}]}},
    {"type": "user", "uuid": "r1", "timestamp": "2026-10-12T08:00:04.000Z",
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                              "content": "Launching skill: alpha"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:05.000Z",
     "attachment": {"type": "invoked_skills",
                    "skills": [{"name": "alpha",
                                "content": "alpha body text here"}]}},
    {"type": "assistant", "uuid": "a2",
     "timestamp": "2026-10-12T08:00:06.000Z",
     "message": {"content": [
         {"type": "tool_use", "id": "t2", "name": "Read",
          "input": {"file_path": "/ws/.changes/demo/progress.md"}}]}},
    {"type": "user", "uuid": "r2", "timestamp": "2026-10-12T08:00:07.000Z",
     "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                              "content": "current step 3"}]}},
    {"type": "attachment", "timestamp": "2026-10-12T08:00:08.000Z",
     "attachment": {"type": "skill_listing",
                    "content": "- alpha: Use when testing.\n"
                               "- beta: Use when billing.\n"}},
]

DIRECT_LINES = [
    {"type": "user", "uuid": "u1", "timestamp": "2026-10-12T08:00:01.000Z",
     "message": {"content": [{"type": "text", "text": "quick question"}]}},
    {"type": "assistant", "uuid": "a1",
     "timestamp": "2026-10-12T08:00:02.000Z",
     "message": {"content": [{"type": "text", "text": "answer"}]}},
]


class RunAuditProcessTests(unittest.TestCase):
    def setUp(self):
        isolate_uploads(self)

    def test_result_carries_process_block_and_ceremony_row(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "s.jsonl"
            transcript.write_text(
                "\n".join(json.dumps(line) for line in PIPELINE_LINES),
                encoding="utf-8")
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
            finally:
                web.CACHE_DIR = original_cache_dir

            self.assertEqual(job.status, "done")
            process = job.result["process"]
            self.assertEqual(process["style"], "pipeline")
            self.assertEqual(process["workflow"], {"reads": 1, "writes": 0})
            self.assertEqual(process["listing"]["occurrences"], 2)
            self.assertGreater(
                process["listing"]["reinject_tokens_est"], 0)
            self.assertEqual(
                sorted(process),
                ["inject", "interventions", "listing", "notes",
                 "skill_loads", "style", "workflow"])
            keys = {row["key"] for row in job.result["bill"]["cost_rows"]}
            self.assertIn("ceremony", keys)


class FleetProcessGroupsTests(unittest.TestCase):
    def setUp(self):
        isolate_uploads(self)

    def test_sessions_group_by_style_with_ceremony_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "ws"
            workspace.mkdir()
            (workspace / ("a" * 8 + "-1111-1111-1111-111111111111.jsonl")
             ).write_text(
                "\n".join(json.dumps(line) for line in PIPELINE_LINES),
                encoding="utf-8")
            (workspace / ("b" * 8 + "-2222-2222-2222-222222222222.jsonl")
             ).write_text(
                "\n".join(json.dumps(line) for line in DIRECT_LINES),
                encoding="utf-8")
            fleet = web.fleet_stats([str(base)])

        groups = fleet["process_groups"]
        self.assertEqual(sorted(groups), ["direct", "pipeline"])
        self.assertEqual(groups["pipeline"]["sessions"], 1)
        self.assertEqual(groups["direct"]["sessions"], 1)
        self.assertGreater(
            groups["pipeline"]["total_listing_reinject_tokens_est"], 0)
        self.assertEqual(
            groups["direct"]["total_listing_reinject_tokens_est"], 0)
        self.assertGreaterEqual(groups["pipeline"]["avg_turns"], 1)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_web.RunAuditProcessTests tests.test_web.FleetProcessGroupsTests -v`
Expected: FAIL — RunAudit：`KeyError: 'process'`；Fleet：`KeyError: 'process_groups'`

- [ ] **Step 3: 实现 web.py**

3a. `run_audit` import 区（`from .ir.library import library_report` 后）加：

```python
        from .ir.process import process_signature
```

`document = build_audit_document(session, adapter_id)` 之后加：

```python
        process_block = process_signature(document)
```

`build_bill(...)` 调用加末参：

```python
        bill = build_bill(
            token_stats_block, load_block, returns_block, expectations,
            judgments_block, if_block, billed_series,
            len(timeline.file_reads), adapter_id, judge_enabled,
            process=process_block)
```

`job.result` 字典 `"skill_gaps": gap_block,` 之后加：

```python
            "process": process_block,
```

3b. `fleet_stats` 改造：fleet 初始字典加 `"process_groups": {}`；`except Exception` 块加一行 `s["process"] = None`；`if s["turns"] is None: continue` 之后、既有累积之前加：

```python
        # Signature machinery sits in its own guard: an IR-build failure
        # degrades only this feature, not the session's other stats.
        try:
            document = build_audit_document(
                session, _ir_adapter_id(session, Path(s["path"])))
            s["process"] = process_signature(document)
        except Exception:
            s["process"] = None
```

（函数顶部 import：`from .ir.builder import build_audit_document` 与 `from .ir.process import process_signature`。）

既有累积之后（`for cls, extra in ...` 循环后）加：

```python
        sig = s["process"]
        if sig:
            group = fleet["process_groups"].setdefault(sig["style"], {
                "sessions": 0, "turns_sum": 0, "avg_turns": None,
                "with_usage": 0, "hit_sum": 0.0,
                "avg_cache_hit_rate": None, "total_output_tokens": 0,
                "total_repeat_extra_tokens": 0, "repeat_class_totals": {},
                "total_listing_reinject_tokens_est": 0,
                "total_ceremony_tokens_est": 0, "total_compactions": 0,
                "interventions_signal_sum": 0,
                "avg_interventions_signal": None})
            group["sessions"] += 1
            group["turns_sum"] += s["turns"]
            if s["cache_hit_rate"] is not None:
                group["with_usage"] += 1
                group["hit_sum"] += s["cache_hit_rate"]
            group["total_output_tokens"] += s["output_total"] or 0
            group["total_repeat_extra_tokens"] += \
                s["repeat_extra_tokens"] or 0
            for cls, extra in (s["repeat_class_totals"] or {}).items():
                group["repeat_class_totals"][cls] = \
                    group["repeat_class_totals"].get(cls, 0) + extra
            listing_extra = sig["listing"]["reinject_tokens_est"]
            protocol = (s["repeat_class_totals"] or {}) \
                .get("post_compaction") or 0
            group["total_listing_reinject_tokens_est"] += listing_extra
            group["total_ceremony_tokens_est"] += listing_extra + protocol
            group["total_compactions"] += len(stats.compaction_points)
            group["interventions_signal_sum"] += \
                sig["interventions"]["signal"]
```

函数末尾（`if fleet["with_usage"]:` 块后）加：

```python
    for group in fleet["process_groups"].values():
        if group["sessions"]:
            group["avg_turns"] = round(
                group["turns_sum"] / group["sessions"], 1)
            group["avg_interventions_signal"] = round(
                group["interventions_signal_sum"] / group["sessions"], 1)
        if group["with_usage"]:
            group["avg_cache_hit_rate"] = round(
                group["hit_sum"] / group["with_usage"], 3)
        del group["turns_sum"], group["hit_sum"], \
            group["interventions_signal_sum"]
```

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_web -v 2>&1 | tail -5`
Expected: PASS（含原有 fleet/billed 测试——`process=None` 会话不进组）

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: web audit carries process; fleet groups by style"
```

---

### Task 5: report + CLI — 流程签名节

**Files:**
- Modify: `src/agent_session_detective/report.py`（`render_report` 签名 + Skill GAP 节后插节）
- Modify: `src/agent_session_detective/cli.py`（import + 主流程 + render_report 调用）
- Test: `tests/test_report_injected.py`（追加）、`tests/test_cli.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/test_report_injected.py` 末尾追加（文件已有 `FIXTURES`/`shutil`/`tempfile`/`Path`/`render_report`/`load_session`/`build_timeline` import）：

```python
def process_block(style="pipeline"):
    return {
        "style": style,
        "inject": {"items": 3, "tokens_est": 8400},
        "listing": {"skills": 37, "occurrences": 74,
                    "reinject_occurrences": 37, "reinject_tokens_est": 4200},
        "skill_loads": {"loads": 5, "reloads": 2},
        "workflow": {"reads": 14, "writes": 6},
        "interventions": {"total": 20, "signal": 8},
        "notes": [],
    }


class ProcessSectionTests(unittest.TestCase):
    def render_with_process(self, process, token_stats=None):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "t.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            session = load_session(transcript)
            return render_report(
                session, build_timeline(session), [],
                judge_enabled=True, catalog_size=6,
                process=process, token_stats=token_stats)

    def test_section_renders_style_and_features(self):
        html = self.render_with_process(process_block())
        self.assertIn("流程签名", html)
        self.assertIn("pipeline", html)
        self.assertIn("14 读", html)
        self.assertIn("4200", html)

    def test_section_renders_ceremony_with_protocol_reread(self):
        from agent_session_detective.tokenstats import TokenStats
        stats = TokenStats(repeat_class_totals={"post_compaction": 900})
        html = self.render_with_process(process_block(), token_stats=stats)
        self.assertIn("ceremony ~5100", html)
        self.assertIn("协议重读 ~900", html)

    def test_note_lines_render(self):
        block = process_block("mixed")
        block["notes"] = ["no skill listings observed — listing "
                          "re-injection is unmeasurable"]
        html = self.render_with_process(block)
        self.assertIn("unmeasurable", html)
```

`tests/test_cli.py` 末尾追加（文件已有 `FIXTURES`/`shutil`/`tempfile`/`Path`/`patch`/`cli` import）：

```python
class ProcessWiringTests(unittest.TestCase):
    def test_cli_passes_process_block_to_report(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "foo.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            process = render.call_args.kwargs["process"]
            self.assertIn(process["style"], ("pipeline", "direct", "mixed"))
            self.assertEqual(
                sorted(process),
                ["inject", "interventions", "listing", "notes",
                 "skill_loads", "style", "workflow"])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m unittest tests.test_report_injected.ProcessSectionTests tests.test_cli.ProcessWiringTests -v`
Expected: FAIL — report：`TypeError: render_report() got an unexpected keyword argument 'process'`；CLI 同因

- [ ] **Step 3: 实现 report.py**

3a. `render_report` 签名 `gap: Optional[dict] = None,` 后加：

```python
    process: Optional[dict] = None,
```

3b. Skill GAP 节末尾（`# --- Instruction Following ---` 之前）插入：

```python
    # --- Process signature (direct vs pipeline machinery) ---
    # An observational lens, not a verdict about which flow is better:
    # machinery features are log facts, the style is a presence rule, and
    # confounds (task difficulty) are stated rather than controlled.
    if process is not None:
        wf = process["workflow"]
        listing = process["listing"]
        inject = process["inject"]
        loads = process["skill_loads"]
        inter = process["interventions"]
        protocol = 0
        if token_stats is not None:
            protocol = token_stats.repeat_class_totals.get(
                "post_compaction") or 0
        ceremony = listing["reinject_tokens_est"] + protocol
        parts.append("<h2>流程签名</h2>")
        parts.append(
            "<p><span class='badge'>style: %s</span>"
            "<span class='badge'>workflow 触碰: %d 读 / %d 写</span>"
            "<span class='badge'>skill loads: %d (%d reload)</span>"
            "<span class='badge'>inject: %d 项 ~%d tokens</span>"
            "<span class='badge'>干预: %d (%d 信号)</span></p>"
            % (esc(process["style"]), wf["reads"], wf["writes"],
               loads["loads"], loads["reloads"],
               inject["items"], inject["tokens_est"],
               inter["total"], inter["signal"]))
        parts.append(
            "<p class='dim'>listing: %d skill / %d 次（重注入 %d 次 ~%d tokens）"
            " · ceremony ~%d tokens = listing 重注入 ~%d + 协议重读 ~%d"
            "（透镜口径：这部分已分别含在上下文增长与重复注入税内）</p>"
            % (listing["skills"], listing["occurrences"],
               listing["reinject_occurrences"],
               listing["reinject_tokens_est"],
               ceremony, listing["reinject_tokens_est"], protocol))
        for note in process["notes"]:
            parts.append("<p class='dim'>%s</p>" % esc(note))
        parts.append(
            "<p class='dim'>观测对比非随机对照：任务难度等混杂未控制；"
            "跨会话对比看 fleet 分组，配对结论需同任务直通/pipeline 各跑一次。</p>")
```

- [ ] **Step 4: 实现 cli.py**

import 区 `from .ir.library import library_report` 后加：

```python
from .ir.process import process_signature
```

主流程 `gap = skill_gaps(...)` 之后加：

```python
    process_block = process_signature(document)
```

`render_report(...)` 调用 `gap=gap,` 后加一行：

```python
        process=process_block,
```

- [ ] **Step 5: 运行确认通过 + 回归**

Run: `python -m unittest tests.test_report_injected tests.test_cli -v`
Expected: PASS

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/report.py src/agent_session_detective/cli.py tests/test_report_injected.py tests/test_cli.py
git commit -m "feat: process signature section in CLI report"
```

---

### Task 6: webapp — process 面板 + fleet 分组表

**Files:**
- Modify: `src/agent_session_detective/webapp/app.js`（HELP 字典、单会话渲染区、renderFleet）

- [ ] **Step 1: 语法基线**

Run: `node --check src/agent_session_detective/webapp/app.js`
Expected: 无输出（确认改动前基线干净）

- [ ] **Step 2: HELP 加 "process" 键**

HELP 对象 `"gaps": {...}` 条目后追加（注意给 `"gaps"` 条目末尾补逗号）：

```js
    "process":
      "流程签名：这个会话跑在多少流程机器上。style = pipeline（workflow 状态文件读写 + skill 加载都在场）/" +
      "direct（都没有）/mixed（只有一半）。inject = harness 注入体量；listing 重注入 = 同一 skill 目录被反复塞回上下文；" +
      "ceremony = listing 重注入 + 压缩协议重读的透镜口径（已含在上下文增长/重复注入税内，不另计钱）。" +
      "直通 vs pipeline 是观测对比非随机对照，任务难度混杂未控制。"
```

- [ ] **Step 3: 单会话面板**

`parts.push(renderTokenStats(r.token_stats));`（1433 行附近）之前插入：

```js
    if (r.process) {
      var ps = r.process;
      parts.push(
        "<section id='process'><h2>process signature " + helpDot("process") + "</h2>" +
        "<p>" +
        badge(ps.style === "pipeline" ? "warn" : (ps.style === "direct" ? "ok" : "dim"),
              "style: " + esc(ps.style)) +
        badge("dim", "workflow " + ps.workflow.reads + " 读 / " + ps.workflow.writes + " 写") +
        badge("dim", "loads " + ps.skill_loads.loads + " (" + ps.skill_loads.reloads + " reload)") +
        badge("dim", "inject " + ps.inject.items + " 项 ~" + ps.inject.tokens_est + " tok") +
        badge("dim", "干预 " + ps.interventions.signal + "/" + ps.interventions.total) +
        "</p>" +
        "<p class='dim'>listing " + ps.listing.skills + " skill / " + ps.listing.occurrences +
        " 次 · 重注入 " + ps.listing.reinject_occurrences + " 次 ~" +
        ps.listing.reinject_tokens_est + " tok · 观测对比非随机对照（任务难度混杂未控制）</p>" +
        ps.notes.map(function (n) {
          return "<p class='dim'>" + esc(n) + "</p>";
        }).join("") +
        "</section>");
    }
```

- [ ] **Step 4: renderFleet 分组表**

`renderFleet` 的 `box.innerHTML = (...)` 拼接末尾追加（在 repeat 徽章字符串之后、闭合引号之前接 `+`）：

```js
      (function () {
        var groups = fleet.process_groups || {};
        var keys = Object.keys(groups);
        if (!keys.length) { return ""; }
        var rows = keys.map(function (k) {
          var g = groups[k];
          return "<tr><td>" + esc(k) + "</td><td>" + g.sessions + "</td><td>" +
            (g.avg_turns == null ? "—" : g.avg_turns) + "</td><td>" +
            (g.avg_cache_hit_rate == null
              ? "—" : Math.round(g.avg_cache_hit_rate * 100) + "%") + "</td><td>" +
            (g.total_ceremony_tokens_est || 0) + "</td><td>" +
            (g.total_repeat_extra_tokens || 0) + "</td><td>" +
            (g.total_compactions || 0) + "</td><td>" +
            (g.avg_interventions_signal == null
              ? "—" : g.avg_interventions_signal) + "</td></tr>";
        }).join("");
        return '<span class="dim" style="flex-basis:100%">' +
          "process（观测分组 · 非随机对照，任务难度混杂未控制）</span>" +
          "<table><tr><th>style</th><th>sessions</th><th>avg turns</th>" +
          "<th>avg cache</th><th>ceremony tok</th><th>repeat +</th>" +
          "<th>compactions</th><th>avg 干预</th></tr>" + rows + "</table>";
      })()
```

- [ ] **Step 5: 语法校验 + 全量回归**

Run: `node --check src/agent_session_detective/webapp/app.js`
Expected: 无输出

Run: `python -m unittest discover -s tests 2>&1 | tail -3`
Expected: OK

- [ ] **Step 6: 手动验证（浏览器）**

`python -m agent_session_detective.web`（或 README 启动命令）起 web，跑一个真实 Qoder 会话审计：确认 process 面板渲染、helpDot 弹出 "process" 文案、fleet 按钮出分组表。无法起浏览器则明确报告未做此步。

- [ ] **Step 7: Commit**

```bash
git add src/agent_session_detective/webapp/app.js
git commit -m "feat: process + fleet process panels in web UI"
```

---

## 完成定义

- `python -m unittest discover -s tests` 全绿
- 真实 Qoder 会话：`asd <transcript> --no-judge` 报告含「流程签名」节（style/特征/ceremony/混杂声明）
- web payload 含 `process` 键，bill 出现 ceremony 透镜行（listing 重注入 >0 的会话）
- fleet 端点返回 `process_groups`，UI 渲染分组对比表与混杂警示
- 判分链路（judge prompt / IF / gap 候选池）零改动——A/B 被测对象未中途变化
