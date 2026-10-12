# Upload Sessions + Inline-Sidechain Join Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web 应用支持上传 jsonl 会话文件（持久化、进 sidebar、与扫描会话同构分析），并修复内联 sidechain 转录的 dispatch→子代理 join（snake_case key + `sidechain:` 代理分类 + uuid 前缀解析）。

**Architecture:** 上传文件落 `~/.local/share/agent-session-detective/uploads/`（sha1 清单去重），发现层把该目录扫成普通会话条目——web 全链路（audit 表单/报告/tree/缓存）只认 `path`，零改动。保真修复在 wire 层（双 casing key）、IR 分类层（`sidechain:` 前缀进 join 循环与 returns 台账）、billing 层（uuid 前缀 stem）。

**Tech Stack:** Python stdlib only（http.server、tempfile、hashlib、re）；前端 vanilla JS 单页（webapp/）。测试 unittest + pytest。

**Spec:** `docs/superpowers/specs/2026-10-12-upload-sessions-design.md`

**Test command:** `PYTHONPATH=src python3 -m pytest tests/ -q`（单测：`PYTHONPATH=src python3 -m pytest tests/test_wire.py -q` 等）

**关键背景（实现者必读）:**

1. 两种 Qoder 子代理数据形状：
   - 多文件形（本机）：sidechain 记录拆到 `<session>/subagents/*.jsonl`，join key 为 camelCase `parentToolUseId`（子代理文件记录上）或 meta sidecar `toolUseId`
   - **单文件形（上传目标，v1.1.62 导出）**：sidechain 记录内联在主 transcript，join key 为 **snake_case `parent_tool_use_id`**，代理归因靠 parentUuid 链产生 `sidechain:<root-uuid>` 代理 id
2. 当前三处断点（都已实测确认）：
   - `wire.py:416` 只读 camelCase → ref 里 `parent_tool_use_id` 恒 None
   - `ir/dispatch.py:41` `_is_subagent` 只认 `subagent:` 前缀 → `sidechain:` 代理根本不进 join 循环
   - `ir/analyses.py:328` `subagent_returns` 同样只认 `subagent:` → 内联代理的 process token 不入台账
3. `billing.py:38` `session_uuid_from_source` 只接受纯 uuid stem → `uuid-木晓-20261010.jsonl` 这类名字让 billed/IDE-DB 侧通道静默失效（`ide_db.py` 也走这个函数，修一处两边生效）
4. web.py 模块级常量 `CACHE_DIR` 的 patch 模式（`patch.object(web, "CACHE_DIR", ...)`）同样适用于新常量 `UPLOADS_DIR`
5. IR_VERSION bump 会让 web 审计缓存自失效（fingerprint 含 `ir<IR_VERSION>`，见 `web.py:118-129` 注释），这是设计好的机制，不要另改 v11

---

### Task 1: wire 层读 snake_case `parent_tool_use_id`

**Files:**
- Modify: `src/agent_session_detective/wire.py:412-422`（`_qoder_ref`）
- Test: `tests/test_wire.py`（`QoderTranscriptTests` 类内，加在 `test_attaches_source_record_identity_to_every_event_from_that_record` 之后，约 line 356）

- [ ] **Step 1: Write the failing test**

```python
    def test_parent_tool_use_id_reads_snake_case_key(self):
        # Two key spellings: the multi-file shape writes camelCase
        # parentToolUseId on subagent-file records, the merged single-file
        # shape writes snake_case parent_tool_use_id on inline sidechain
        # records. Both must surface in the ref.
        records = [
            {"type": "user", "timestamp": "2026-10-10T08:00:00.000Z",
             "uuid": "s1", "parentUuid": None, "isSidechain": True,
             "parent_tool_use_id": "call_inline",
             "message": {"content": [{"type": "text", "text": "brief"}]}},
            {"type": "assistant", "timestamp": "2026-10-10T08:00:01.000Z",
             "uuid": "s2", "parentUuid": "s1", "isSidechain": True,
             "parentToolUseId": "call_camel",
             "message": {"model": "m", "content": [{"type": "text", "text": "hi"}]}},
        ]
        events = self._load_records(records)

        self.assertEqual(events[0].ref["parent_tool_use_id"], "call_inline")
        self.assertEqual(events[1].ref["parent_tool_use_id"], "call_camel")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_wire.py -q -k snake_case`
Expected: FAIL — `events[0].ref["parent_tool_use_id"]` is `None` not `"call_inline"`（camelCase 记录会通过，snake_case 断言失败）

- [ ] **Step 3: Write minimal implementation**

`wire.py` `_qoder_ref` 返回 dict 中的一行（line 416）：

```python
        "is_sidechain": record.get("isSidechain"),
        # CamelCase was the multi-file shape's key; the merged single-file
        # runtime shape writes snake_case on inline sidechain records.
        "parent_tool_use_id": record.get(
            "parentToolUseId", record.get("parent_tool_use_id")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m pytest tests/test_wire.py -q`
Expected: PASS（全文件）

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/wire.py tests/test_wire.py
git commit -m "fix: read snake_case parent_tool_use_id from inline sidechain records"
```

---

### Task 2: `sidechain:` 代理进入 join 循环与 returns 台账

**Files:**
- Modify: `src/agent_session_detective/ir/records.py`（`attribute_agents` 后新增 `is_subagent_agent`）
- Modify: `src/agent_session_detective/ir/dispatch.py:41-42`（`_is_subagent` 委托）+ line 35 import
- Modify: `src/agent_session_detective/ir/analyses.py:328`（`subagent_returns` 过滤）+ import
- Test: `tests/test_ir_records.py`（新测试）、`tests/test_ir_dispatch.py`（新 fixture + 新测试类）

- [ ] **Step 1: Write the failing unit test（records 分类）**

`tests/test_ir_records.py` 中新增（跟随该文件既有 import；需要 `from agent_session_detective.ir.records import is_subagent_agent`）：

```python
    def test_is_subagent_agent_covers_both_channels(self):
        # "subagent:<stem>" = separate transcript files;
        # "subagent:ide-db:<id>" = IDE-DB synthesized;
        # "sidechain:<root-uuid>" = inline records in the merged shape.
        self.assertTrue(is_subagent_agent("subagent:worker"))
        self.assertTrue(is_subagent_agent("subagent:ide-db:abc"))
        self.assertTrue(is_subagent_agent("sidechain:s1"))
        self.assertFalse(is_subagent_agent("main"))
        self.assertFalse(is_subagent_agent("sidechain"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_records.py -q -k is_subagent_agent`
Expected: FAIL — `ImportError: cannot import name 'is_subagent_agent'`

- [ ] **Step 3: Implement the helper**

`src/agent_session_detective/ir/records.py`，`attribute_agents` 函数之后新增：

```python
def is_subagent_agent(agent_id: str) -> bool:
    """Both subagent channels produce dispatchable agents: separate
    transcript files ("subagent:<stem>", "subagent:ide-db:<id>") and
    sidechain records merged into the main transcript
    ("sidechain:<root-uuid>")."""
    return agent_id.startswith(("subagent:", "sidechain:"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_records.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing e2e test（dispatch join + returns）**

`tests/test_ir_dispatch.py`，在 `single_dispatch_session` 之后新增 fixture，在 `DispatchJoinTest` 类之后新增测试类：

```python
def inline_sidechain_session(test):
    """The merged single-file shape: sidechain records inline in the main
    transcript, joined by snake_case parent_tool_use_id — no subagent files,
    no sidecars."""
    ts = _clock()
    lines = [
        user_record(ts(), "m1", "kick off"),
        assistant_record(
            ts(), "m2",
            tool_use=("call_inline", "Agent",
                      {"subagent_type": "code", "description": "merged worker"}),
            request_id="req-1", parent="m1"),
        {"type": "user", "uuid": "s1", "parentUuid": None, "isSidechain": True,
         "timestamp": ts(), "parent_tool_use_id": "call_inline",
         "message": {"content": [{"type": "text", "text": "alpha-flow do the work"}]}},
        {"type": "assistant", "uuid": "s2", "parentUuid": "s1", "isSidechain": True,
         "timestamp": ts(),
         "requestTokenAnchor": {"request": "r" * 64, "response": "s" * 64,
                                "requestId": "req-s1"},
         "message": {"model": "qoder-pro",
                     "content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": 900, "output_tokens": 5,
                               "request_id": "req-s1"}}},
    ]
    return test.load_lines(lines)


class InlineSidechainJoinTest(IREventsTestCase, unittest.TestCase):
    def test_inline_sidechain_joins_via_snake_case_parent_tool_use_id(self):
        document = build_audit_document(inline_sidechain_session(self), "qoder")

        self.assertEqual(len(document.dispatches), 1)
        dispatch = document.dispatches[0]
        self.assertEqual(dispatch.subagent_agent_id, "sidechain:s1")
        self.assertEqual(dispatch.subagent_type, "code")
        self.assertIsNotNone(dispatch.brief_item_id)

    def test_inline_sidechain_counts_as_a_subagent_process(self):
        from agent_session_detective.ir.analyses import subagent_returns
        document = build_audit_document(inline_sidechain_session(self), "qoder")

        returns = subagent_returns(document)

        self.assertEqual(returns["totals"]["linked"], 1)
        self.assertEqual(returns["totals"]["process_unavailable"], 0)
        self.assertIsNotNone(returns["rows"][0]["subagent_tokens_est"])
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_dispatch.py -q -k InlineSidechain`
Expected: FAIL — `dispatch.subagent_agent_id` is `None`（`_is_subagent("sidechain:s1")` 为 False，join 循环跳过了内联代理；第二个测试 `subagent_tokens_est` 为 None / `linked == 0`）

- [ ] **Step 7: Broaden the two classification points**

`src/agent_session_detective/ir/dispatch.py`：

```python
from .records import AgentRecords, is_subagent_agent
```

（替换原 line 35 `from .records import AgentRecords`）

```python
def _is_subagent(agent_id: str) -> bool:
    return is_subagent_agent(agent_id)
```

`src/agent_session_detective/ir/analyses.py`（顶部 import 区加）：

```python
from .records import is_subagent_agent
```

line 328：

```python
    for item in document.items:
        if is_subagent_agent(item.agent_id):
            sub_items.setdefault(item.agent_id, []).append(item)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_dispatch.py tests/test_ir_records.py tests/test_ir_analyses.py -q`
Expected: PASS（若既有测试曾固化「sidechain 不算子代理」的旧断言而失败，更新该断言——join 本该覆盖两渠道）

- [ ] **Step 9: Run the real-world regression（用真实上传形状）**

用本机 Downloads 的真实文件验证 11 条派发全部 join（该文件 4456 行、11 个 distinct parent_tool_use_id 与 11 个 main 线 Agent tool_use id 完全匹配）：

```bash
PYTHONPATH=src python3 - <<'EOF'
import json
from pathlib import Path
from agent_session_detective import wire
from agent_session_detective.ir.builder import build_audit_document
session = wire.load_session(Path("/Users/wangting/Downloads/53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010.jsonl"))
document = build_audit_document(session, "qoder")
joined = [d for d in document.dispatches if d.subagent_agent_id]
print("dispatches:", len(document.dispatches), "joined:", len(joined))
assert len(document.dispatches) == 11 and len(joined) == 11, "join incomplete"
print("all 11 inline sidechains joined")
EOF
```

Expected: `dispatches: 11 joined: 11` / `all 11 inline sidechains joined`

- [ ] **Step 10: Commit**

```bash
git add src/agent_session_detective/ir/records.py src/agent_session_detective/ir/dispatch.py src/agent_session_detective/ir/analyses.py tests/test_ir_records.py tests/test_ir_dispatch.py
git commit -m "fix: join inline sidechain agents into dispatches and returns ledger"
```

---

### Task 3: IR_VERSION 1.7 → 1.8 + pin 更新 + 形状文档

**Files:**
- Modify: `src/agent_session_detective/ir/schema.py:15`
- Modify: `tests/test_ir_schema.py:124,135`、`tests/test_ir_dispatch.py:414-415`、`tests/test_ir_builder.py:263`、`tests/test_cli.py:186`
- Modify: `docs/qoder-transcript-shape.md`

- [ ] **Step 1: Update the version pins in tests**

把以下五处 `"1.7"` 改为 `"1.8"`（同一 commit 内测试先行会红，属预期，实现步骤 3 使其回绿）：

- `tests/test_ir_schema.py:124`：`self.assertEqual(IR_VERSION, "1.8")`
- `tests/test_ir_schema.py:135`：`self.assertEqual(revived["ir_version"], "1.8")`
- `tests/test_ir_dispatch.py:414`：`self.assertEqual(IR_VERSION, "1.8")`
- `tests/test_ir_dispatch.py:415`：`self.assertEqual(audit(self).ir_version, "1.8")`
- `tests/test_ir_builder.py:263`：`self.assertEqual(document.ir_version, "1.8")`
- `tests/test_cli.py:186`：`self.assertEqual(document["ir_version"], "1.8")`

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_schema.py -q -k version`
Expected: FAIL（仍是 1.7）

- [ ] **Step 3: Bump the version**

`src/agent_session_detective/ir/schema.py` line 15：

```python
# IR 1.8: inline-sidechain dispatches join in-band (snake_case
# parent_tool_use_id + sidechain: agent classification), so dispatch rows
# that previously rendered as orphans now carry subagent_agent_id and the
# returns ledger counts their process tokens. Cached web results must
# recompute (fingerprint embeds ir<IR_VERSION>).
IR_VERSION = "1.8"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_ir_schema.py tests/test_ir_dispatch.py tests/test_ir_builder.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: Document the merged single-file shape**

`docs/qoder-transcript-shape.md` 末尾新增一节（该文档是 Qoder 落盘形状的对外权威页）：

```markdown
## 单文件合并形（2026-10-12 实测，导出 transcript）

另一台机器的 v1.1.62 运行时写出第三种布局：**sidechain 记录内联在主
transcript 里**，不拆 `subagents/*.jsonl`。实测样本 4456 行 / 91% 为
sidechain 记录：

- join key 为 **snake_case `parent_tool_use_id`**（多文件形写 camelCase
  `parentToolUseId` 在子代理文件记录上）；同文件还有 `agentId`
  （`aauxpca-<type>-<hash>` 形，内嵌 subagent_type 佐证）
- 无 `system/compact_boundary`、无 `attachment` 记录（样本中均 0 条），
  usage/anchor 遥测完整（1024/1024 requests 带 FACT）
- 代理归因走 parentUuid 链（`sidechain:<root-uuid>`）；ASD wire 层自
  IR 1.8 起双 casing 读取 join key，dispatch join 与 returns 台账对
  `sidechain:` 代理生效
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_session_detective/ir/schema.py tests/test_ir_schema.py tests/test_ir_dispatch.py tests/test_ir_builder.py tests/test_cli.py docs/qoder-transcript-shape.md
git commit -m "feat: bump the IR to 1.8 for the inline-sidechain join and document the merged shape"
```

---

### Task 4: billing 层 uuid 前缀 stem 解析

**Files:**
- Modify: `src/agent_session_detective/billing.py:27-44`
- Test: `tests/test_billing.py`（`session_uuid_from_source` 测试类内，`test_uppercase_uuid_stem_normalizes_to_lower` 之后）

- [ ] **Step 1: Write the failing test**

```python
    def test_uuid_prefixed_stem_yields_the_leading_uuid(self):
        # Exported transcripts carry display suffixes after the uuid
        # ("<uuid>-木晓-20261010"); the DB join key is the leading uuid,
        # and both billed usage and the IDE-DB chain route through this.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (UUID + "-木晓-20261010.jsonl")
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(session_uuid_from_source(source), UUID)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_billing.py -q -k prefixed`
Expected: FAIL — returns `None`（正则要求整串是 uuid）

- [ ] **Step 3: Implement the prefix regex**

`src/agent_session_detective/billing.py` 把 `_UUID_RE`（lines 27-30）与 `session_uuid_from_source`（lines 38-44）替换为（`_UUID_RE` 全文件仅此一处使用，删除安全）：

```python
_UUID_STEM_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:[-_.].+)?$",
    re.IGNORECASE,
)
```

```python
def session_uuid_from_source(source_path: Path) -> Optional[str]:
    """The SharedClientCache join key: the Qoder transcript's uuid stem.

    The stem may carry a display suffix after the uuid (exported transcripts
    like ``<uuid>-木晓-20261010``); the leading uuid is the join key.
    """
    source_path = Path(source_path)
    if not source_path.is_file() or source_path.suffix != ".jsonl":
        return None
    match = _UUID_STEM_RE.match(source_path.stem)
    return match.group(1).lower() if match else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_billing.py tests/test_ide_db.py -q`
Expected: PASS（纯 uuid、大写归一、非 uuid、目录四个既有用例不回归）

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/billing.py tests/test_billing.py
git commit -m "fix: accept uuid-prefixed stems in the billed/ide-db join key"
```

---

### Task 5: uploads 存储层（清单、净化、去重、删除）

**Files:**
- Modify: `src/agent_session_detective/web.py`（`CACHE_DIR` 定义之后新增常量；`cached_paths` 之后新增存储函数）
- Modify: `src/agent_session_detective/web.py` 顶部 import（加 `import tempfile`）
- Test: `tests/test_web.py`（新测试类 `UploadStoreTests`）

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py` 新增（该文件已 import `web`、`json`、`shutil`、`tempfile`、`Path`、`unittest`）：

```python
def qoder_transcript_text():
    record = {
        "type": "user", "timestamp": "2026-10-10T08:00:00.000Z",
        "uuid": "u1", "parentUuid": None, "isSidechain": False,
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    return json.dumps(record) + "\n"


class UploadStoreTests(unittest.TestCase):
    def setUp(self):
        self._orig = web.UPLOADS_DIR
        web.UPLOADS_DIR = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, web.UPLOADS_DIR, True)
        self.addCleanup(setattr, web, "UPLOADS_DIR", self._orig)

    def test_store_upload_persists_file_and_manifest(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"),
            "53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010.jsonl")

        session = result["session"]
        self.assertFalse(result["dedup"])
        self.assertTrue(session["uploaded"])
        self.assertEqual(session["workspace"], "upload")
        self.assertEqual(session["id"],
                         "53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010")
        stored = Path(session["path"])
        self.assertEqual(stored.parent, web.UPLOADS_DIR)
        self.assertTrue(stored.is_file())
        self.assertIn(stored.name, web.load_upload_manifest())

    def test_same_content_dedupes_to_one_file(self):
        body = qoder_transcript_text().encode("utf-8")
        first = web.store_upload(body, "one.jsonl")
        second = web.store_upload(body, "two.jsonl")

        self.assertFalse(first["dedup"])
        self.assertTrue(second["dedup"])
        self.assertEqual(first["session"]["path"], second["session"]["path"])
        self.assertEqual(len(list(web.UPLOADS_DIR.glob("*.jsonl"))), 1)

    def test_upload_name_never_escapes_the_uploads_dir(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"), "../../evil.jsonl")

        stored = Path(result["session"]["path"])
        self.assertEqual(stored.parent, web.UPLOADS_DIR)
        self.assertEqual(stored.name, "evil.jsonl")
        self.assertEqual(
            list(p.name for p in web.UPLOADS_DIR.glob("*.jsonl")), ["evil.jsonl"])

    def test_unparseable_payload_is_rejected_and_leaves_no_file(self):
        with self.assertRaises(web.UploadRejected):
            web.store_upload(b"not a session at all", "bad.jsonl")

        self.assertEqual(list(web.UPLOADS_DIR.glob("*.jsonl")), [])

    def test_delete_upload_removes_file_and_manifest_entry(self):
        result = web.store_upload(
            qoder_transcript_text().encode("utf-8"), "gone.jsonl")
        stored = Path(result["session"]["path"])

        self.assertEqual(web.delete_upload(str(stored)), "gone.jsonl")
        self.assertFalse(stored.exists())
        self.assertNotIn("gone.jsonl", web.load_upload_manifest())

    def test_delete_upload_refuses_paths_outside_the_uploads_dir(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "rollout-x.jsonl"
            outside.write_text("{}", encoding="utf-8")

            self.assertIsNone(web.delete_upload(str(outside)))
            self.assertTrue(outside.exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q -k UploadStore`
Expected: FAIL — `AttributeError: module 'agent_session_detective.web' has no attribute 'UPLOADS_DIR'`

- [ ] **Step 3: Implement the storage layer**

`src/agent_session_detective/web.py`：

(a) 顶部 import 增加一行（与既有 import 并列）：

```python
import tempfile
```

(b) `CACHE_DIR = ...`（line 51）之后新增：

```python
UPLOADS_DIR = Path("~/.local/share/agent-session-detective/uploads").expanduser()
MAX_UPLOAD_BYTES = 1 << 30  # 1 GiB


class UploadRejected(Exception):
    """The payload is not an analyzable session file (bad format, no events)."""
```

(c) `cached_paths()` 函数之后新增：

```python
# --------------------------------------------------------------------------
# uploaded sessions: user-provided transcript files, persisted under
# UPLOADS_DIR and analyzed exactly like scanned sessions (the whole audit
# flow keys off the file path).
# --------------------------------------------------------------------------

def _manifest_path() -> Path:
    return UPLOADS_DIR / "uploads.json"


def load_upload_manifest() -> Dict[str, dict]:
    try:
        manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _save_manifest(manifest: Dict[str, dict]) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _manifest_path().write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _sanitize_upload_name(name: str) -> str:
    """A filesystem-safe stem from the original filename. Path separators
    and control characters are stripped so an upload name never escapes
    the uploads directory."""
    stem = Path(name.replace("\\", "/")).name
    stem = "".join(ch for ch in stem if ch.isprintable())
    stem = stem.rsplit(".", 1)[0].strip(" .") or "upload"
    return stem[:120]


def _upload_entry(stored: str) -> dict:
    path = UPLOADS_DIR / stored
    return {
        "id": path.stem,
        "path": str(path),
        "workspace": "upload",
        "mtime": path.stat().st_mtime,
        "uploaded": True,
    }


def store_upload(body: bytes, original_name: str) -> dict:
    """Persist one uploaded session file; returns the discovery-shaped
    session entry plus ``dedup`` and ``bytes``.

    The payload must parse through the same ``load_session`` the audit
    flow uses — a file that yields zero events is rejected rather than
    stored as a dead sidebar row. Same content (sha1) re-uploads map to
    the existing file instead of a duplicate row.
    """
    digest = hashlib.sha1(body).hexdigest()
    manifest = load_upload_manifest()
    for stored, entry in manifest.items():
        if entry.get("sha1") == digest and (UPLOADS_DIR / stored).is_file():
            return {"session": _upload_entry(stored), "dedup": True,
                    "bytes": len(body)}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.jsonl"
        probe.write_bytes(body)
        try:
            session = load_session(probe)
            if not session.events:
                raise ValueError("no events parsed")
        except Exception as exc:
            raise UploadRejected(str(exc)) from exc
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stem = _sanitize_upload_name(original_name)
    stored = stem + ".jsonl"
    n = 2
    while (UPLOADS_DIR / stored).exists():
        stored = "%s-%d.jsonl" % (stem, n)
        n += 1
    (UPLOADS_DIR / stored).write_bytes(body)
    manifest[stored] = {
        "original": Path(original_name.replace("\\", "/")).name,
        "sha1": digest,
        "uploaded_at": time.time(),
        "size": len(body),
    }
    _save_manifest(manifest)
    return {"session": _upload_entry(stored), "dedup": False, "bytes": len(body)}


def delete_upload(path_str: str) -> Optional[str]:
    """Remove one uploaded session file. Returns the stored name, or None
    when the path is not an uploaded file (outside the uploads directory,
    wrong suffix, or absent) — a DELETE can never touch scanned sessions."""
    base = UPLOADS_DIR.resolve()
    try:
        target = Path(path_str).expanduser().resolve()
    except OSError:
        return None
    if target.parent != base or target.suffix != ".jsonl" or not target.is_file():
        return None
    stored = target.name
    target.unlink()
    manifest = load_upload_manifest()
    if manifest.pop(stored, None) is not None:
        _save_manifest(manifest)
    return stored
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q`
Expected: PASS（全文件，含既有测试——UPLOADS_DIR 默认目录不存在时存储函数仅在调用时触碰它）

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: persist uploaded session files with a sha1-dedup manifest"
```

---

### Task 6: HTTP 路由 POST/DELETE /api/upload

**Files:**
- Modify: `src/agent_session_detective/web.py`（`do_POST` line ~1021 起、新增 `do_DELETE`）
- Test: `tests/test_web.py`（新测试类 `UploadRouteTests`）

- [ ] **Step 1: Write the failing tests**

`tests/test_web.py` 新增（跟随 `ScanDirsRouteTests` 的服务器模式；`urllib.error` 已 import）：

```python
class UploadRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading

        cls._orig = web.UPLOADS_DIR
        web.UPLOADS_DIR = Path(tempfile.mkdtemp())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(web.UPLOADS_DIR, True)
        web.UPLOADS_DIR = cls._orig

    def test_upload_round_trips_into_the_session_listing(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=" + urllib.parse.quote(
                "53e08e97-70ce-4aff-8647-c49b4f408d83-x.jsonl"),
            data=qoder_transcript_text().encode("utf-8"), method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertFalse(payload["dedup"])
        with patch.object(web, "DEFAULT_ROOTS", []):
            sessions = web.discover_sessions()
        entry = [s for s in sessions if s["path"] == payload["session"]["path"]]
        self.assertEqual(len(entry), 1)
        self.assertTrue(entry[0]["uploaded"])

    def test_garbage_upload_is_rejected_with_400(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=bad.jsonl",
            data=b"garbage", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(list(web.UPLOADS_DIR.glob("*.jsonl")), [])

    def test_delete_removes_an_uploaded_session(self):
        request = urllib.request.Request(
            self.base + "/api/upload?name=del.jsonl",
            data=qoder_transcript_text().encode("utf-8"), method="POST")
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        delete = urllib.request.Request(
            self.base + "/api/upload?path=" + urllib.parse.quote(
                payload["session"]["path"]), method="DELETE")
        with urllib.request.urlopen(delete) as response:
            self.assertEqual(
                json.loads(response.read().decode("utf-8"))["deleted"], "del.jsonl")
        self.assertFalse(Path(payload["session"]["path"]).exists())

    def test_delete_refuses_foreign_paths(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "rollout-x.jsonl"
            outside.write_text("{}", encoding="utf-8")
            request = urllib.request.Request(
                self.base + "/api/upload?path=" + urllib.parse.quote(str(outside)),
                method="DELETE")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(request)
            self.assertEqual(ctx.exception.code, 404)
            self.assertTrue(outside.exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q -k UploadRoute`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: Implement the routes**

`src/agent_session_detective/web.py` `do_POST`（line 1021 起）：在 `if parsed.path in ("/api/scan-dirs", "/api/catalog-audit"):` 块之后、`if parsed.path != "/api/audit":` 之前插入：

```python
        if parsed.path == "/api/upload":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0] or (
                "upload-%d.jsonl" % int(time.time()))
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_UPLOAD_BYTES:
                self._send_json(413, {"error": "upload too large"})
                return
            body = self.rfile.read(length) if length else b""
            try:
                self._send_json(200, store_upload(body, name))
            except UploadRejected as exc:
                self._send_json(
                    400, {"error": "not an analyzable session file: %s" % exc})
            return
```

`do_DELETE` 新方法（`do_POST` 之后、`serve` 之前）：

```python
    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/upload":
            self._send_json(404, {"error": "not found"})
            return
        params = parse_qs(parsed.query)
        path = (params.get("path") or [""])[0]
        if not path:
            self._send_json(400, {"error": "path required"})
            return
        stored = delete_upload(path)
        if stored is None:
            self._send_json(404, {"error": "no such upload"})
            return
        self._send_json(200, {"deleted": stored})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: add /api/upload routes for session upload and delete"
```

---

### Task 7: 发现层合并 uploads

**Files:**
- Modify: `src/agent_session_detective/web.py`（`discover_sessions`，codex 循环之后、`return sorted(...)` 之前——注意在 `for root in roots:` 循环**外**）
- Test: `tests/test_web.py`（新测试类 `UploadDiscoveryTests`）

- [ ] **Step 1: Write the failing test**

```python
class UploadDiscoveryTests(unittest.TestCase):
    def test_uploaded_sessions_join_the_discovery_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            scanned = Path(directory) / "ws" / "transcript.jsonl"
            scanned.parent.mkdir()
            scanned.write_text("{}", encoding="utf-8")
            uploads = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, uploads, True)
            (uploads / "53e08e97-export.jsonl").write_text(
                qoder_transcript_text(), encoding="utf-8")

            with patch.object(web, "UPLOADS_DIR", uploads):
                sessions = web.discover_sessions([str(Path(directory))])

            by_path = {s["path"]: s for s in sessions}
            self.assertTrue(by_path[str(scanned)].get("uploaded") is not True)
            uploaded = by_path[str(uploads / "53e08e97-export.jsonl")]
            self.assertTrue(uploaded["uploaded"])
            self.assertEqual(uploaded["workspace"], "upload")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q -k UploadDiscovery`
Expected: FAIL — uploads 文件不在 listing 里（`by_path` KeyError）

- [ ] **Step 3: Implement the merge**

`discover_sessions` 中，`for root in roots:` 循环结束后（codex 块的缩进层级之外）、`return sorted(...)` 之前插入：

```python
    # Uploaded sessions: user-provided files persisted under UPLOADS_DIR.
    # Same listing shape as scanned sessions; the flag drives the upload
    # badge and the row delete button in the UI.
    if UPLOADS_DIR.is_dir():
        for transcript in UPLOADS_DIR.glob("*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            source_key = "upload:%s" % transcript.resolve()
            if source_key in found and found[source_key]["mtime"] >= mtime:
                continue
            found[source_key] = {
                "id": transcript.stem,
                "path": str(transcript),
                "workspace": "upload",
                "mtime": mtime,
                "uploaded": True,
            }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=src python3 -m pytest tests/test_web.py -q`
Expected: PASS（既有 discovery 测试传自定义 roots + patch 前的 UPLOADS_DIR 默认不存在或为空，不受影响；若本机 uploads 目录已有文件导致某断言计数变化，那是测试隔离问题——确认该测试 patch 了 UPLOADS_DIR）

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: list uploaded sessions alongside scanned ones"
```

---

### Task 8: 前端上传与删除 UI

**Files:**
- Modify: `src/agent_session_detective/webapp/index.html`（sidebar-head 按钮 + 隐藏 input）
- Modify: `src/agent_session_detective/webapp/app.js`（上传/拖拽/删除逻辑 + 行渲染）
- Modify: `src/agent_session_detective/webapp/style.css`（删除按钮 + drop 高亮）

- [ ] **Step 1: index.html — upload 按钮与隐藏 file input**

sidebar-head 的按钮组（line 22-24）变为：

```html
      <div>
        <button class="btn small" id="upload-btn" type="button" title="上传一份 jsonl 会话文件（如别人机器导出的 transcript），落盘后与扫描会话同等分析">upload</button>
        <button class="btn small" id="fleet-btn" type="button" title="跨会话聚合，需要解析全部日志，约 10 秒">fleet</button>
        <button class="btn small" id="refresh" type="button">rescan</button>
      </div>
```

`<input class="session-search" ...>` 之后新增一行：

```html
    <input type="file" id="upload-input" accept=".jsonl" hidden>
```

- [ ] **Step 2: app.js — 上传、拖拽、删除**

在 `document.getElementById("refresh").addEventListener(...)`（约 line 337-339）之后新增：

```js
  // ------------------------------------------------------------------
  // upload: a session file from anywhere (typically an export from
  // another machine) persists in the managed uploads dir and is then
  // just a session with a path — the audit flow treats it identically.
  // ------------------------------------------------------------------

  var uploadInput = document.getElementById("upload-input");
  var uploadBtn = document.getElementById("upload-btn");
  var sidebar = document.querySelector(".sidebar");

  function uploadFile(file) {
    if (!file) return;
    setStatus("uploading " + file.name + "…");
    fetch("/api/upload?name=" + encodeURIComponent(file.name), {
      method: "POST",
      body: file,
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (!res.ok) {
        setStatus("upload failed: " + (res.data.error || "unknown error"));
        return;
      }
      loadSessions();
      openAudit(res.data.session, null);
      setStatus("uploaded " + res.data.session.id +
        (res.data.dedup ? " (already uploaded — reused)" : "") +
        " · pick params and run audit");
    }).catch(function (e) {
      setStatus("upload failed: " + e.message);
    });
  }

  if (uploadBtn) {
    uploadBtn.addEventListener("click", function () { uploadInput.click(); });
  }
  if (uploadInput) {
    uploadInput.addEventListener("change", function () {
      uploadFile(uploadInput.files[0]);
      uploadInput.value = "";
    });
  }
  if (sidebar) {
    sidebar.addEventListener("dragover", function (ev) {
      ev.preventDefault();
      sidebar.classList.add("drop");
    });
    sidebar.addEventListener("dragleave", function () {
      sidebar.classList.remove("drop");
    });
    sidebar.addEventListener("drop", function (ev) {
      ev.preventDefault();
      sidebar.classList.remove("drop");
      uploadFile(ev.dataTransfer && ev.dataTransfer.files[0]);
    });
  }

  function deleteUploaded(session) {
    if (!confirm("删除上传的会话 " + session.id + "？磁盘文件会一并删除。")) return;
    setStatus("deleting " + session.id + "…");
    fetch("/api/upload?path=" + encodeURIComponent(session.path), {
      method: "DELETE",
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function () {
      loadSessions(searchInput.value);
      setStatus("deleted upload " + session.id);
    }).catch(function (e) {
      setStatus("delete failed: " + e.message);
    });
  }
```

- [ ] **Step 3: app.js — 行渲染加 upload 徽标、删除按钮、路径 title**

`loadSessions` 中行渲染（约 line 240-249）替换为：

```js
        el.innerHTML =
          '<div class="session-path">' + esc(s.workspace.replace(/^wd_/, "")) + "</div>" +
          '<div class="session-id">' + esc(s.id.slice(0, 8)) + "</div>" +
          '<div class="session-meta">' +
          badge("dim", fmtTs(s.mtime).slice(5, 16)) +
          (s.uploaded ? badge("ok", "upload") : "") +
          (s.cached ? badge("ok", "audited") : "") +
          "</div>";
        el.title = s.path;
        if (s.uploaded) {
          var del = document.createElement("button");
          del.className = "session-del";
          del.type = "button";
          del.title = "删除这份上传（磁盘文件一并删除）";
          del.textContent = "×";
          del.addEventListener("click", function (ev) {
            ev.stopPropagation();
            deleteUploaded(s);
          });
          el.appendChild(del);
        }
        el.addEventListener("click", function () { openAudit(s, el); });
        sessionList.appendChild(el);
```

- [ ] **Step 4: style.css — 删除按钮与 drop 高亮**

文件末尾追加：

```css
/* uploaded session rows: inline delete affordance + drop target state */
.session-row { position: relative; }
.session-del {
  position: absolute; top: 6px; right: 6px;
  border: none; background: transparent; color: var(--dim);
  font-size: 14px; line-height: 1; cursor: pointer; padding: 2px 5px;
  border-radius: 4px;
}
.session-del:hover { color: var(--bad); background: rgba(245, 80, 76, 0.1); }
.sidebar.drop {
  outline: 2px dashed var(--accent);
  outline-offset: -6px;
  border-radius: 8px;
}
```

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/webapp/index.html src/agent_session_detective/webapp/app.js src/agent_session_detective/webapp/style.css
git commit -m "feat: upload and delete session files from the sidebar"
```

---

### Task 9: 全量测试 + 端到端手工验证

**Files:** 无新改动（验证任务）

- [ ] **Step 1: Full suite**

Run: `PYTHONPATH=src python3 -m pytest tests/ -q`
Expected: 全绿（改前基线 406+ tests；新增 ~13 个）

- [ ] **Step 2: 起服务，CLI 走一遍上传链路（用真实木晓文件）**

```bash
PYTHONPATH=src python3 -m agent_session_detective --serve --port 8472 &
sleep 1
curl -s -X POST --data-binary \
  "/Users/wangting/Downloads/53e08e97-70ce-4aff-8647-c49b4f408d83-木晓-20261010.jsonl" \
  "http://127.0.0.1:8472/api/upload?name=%E6%9C%A8%E6%99%93-20261010.jsonl"
```

Expected: JSON `{"session": {..., "uploaded": true}, "dedup": false, "bytes": 27271394}`；再传一次同样内容 → `dedup: true` 且 `~/.local/share/agent-session-detective/uploads/` 只有一个 jsonl + uploads.json

- [ ] **Step 3: 验证上传会话走完整审计链（含 join）**

```bash
curl -s "http://127.0.0.1:8472/api/sessions" | python3 -m json.tool | grep -A4 '"upload"'
# 记下该会话 path，然后：
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"path": "<上传后的绝对路径>"}' http://127.0.0.1:8472/api/audit
# 轮询 job → status done；skill tree 页:
curl -s "http://127.0.0.1:8472/api/tree?path=<绝对路径>&ide_db=1" | grep -c "sidechain:"
```

Expected: `/api/sessions` 列表含 upload 条目；audit job done；tree 页面 HTML 中出现 11 处 `sidechain:` 派发节点（join 生效的直接证据）。本机 DB 无该会话（别人的机器），`ide_db=1` 优雅降级。

- [ ] **Step 4: 浏览器 UI 验证（agent-browser）**

用 browser-use MCP 打开 `http://127.0.0.1:8472`：
1. sidebar 出现 upload 徽标行，hover 有完整路径 title
2. 点该行 → 审计表单打开，标题显示 `upload · 53e08e97`
3. 拖拽/按钮再上传一个小 jsonl → 出现新行并自动选中
4. 点行内 × → confirm 后行消失，`uploads/` 目录文件被删
5. 跑一次 run audit（不勾 judge）→ 报告渲染正常

- [ ] **Step 5: 收尾**

```bash
# 停服务后确认工作树干净
git status --short
```

Expected: 干净（所有任务已分批提交）。

---

## Self-Review 结论

- **Spec 覆盖**：存储（Task 5）、API 双路由（Task 6）、发现合并+fleet 隐含包含（Task 7——fleet 遍历 discover_sessions，uploads 自动计入）、UI 合并徽标+删除+拖拽（Task 8）、join 修复（Task 1+2）、IR_VERSION（Task 3）、uuid 前缀（Task 4）、错误处理（UploadRejected/413/404/净化，Task 5+6）、测试与手工验证（Task 9）。spec 的「不做什么」均未实现 ✅
- **类型一致**：`store_upload` 返回 `{"session", "dedup", "bytes"}` 在 Task 5/6/8（app.js 读 `res.data.session` / `.dedup`）三处一致；`delete_upload` 返回 `Optional[str]` 与路由 404 分支一致；`is_subagent_agent` 命名在 records/dispatch/analyses 三处一致
- **占位符扫描**：无 TBD/TODO；所有代码步骤给全量代码
