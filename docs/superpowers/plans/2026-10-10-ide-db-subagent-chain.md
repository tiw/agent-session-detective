# IDE DB Subagent Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attach Qoder-IDE subagent chains (stored only in the local SharedClientCache DB, AES-encrypted) to a loaded session so their dispatches join the audit document, resolving orphan dispatches end-to-end with an opt-in flag.

**Architecture:** A new side-channel module `ide_db.py` queries the direct child sessions of the current root session, decrypts each `chat_message.content` (AES-128-CBC, key = IV = `b"QbgzpWzN7tfe43gf"`), and *synthesizes* wire `Event`s — one `WireRecord` per DB row, every event carrying `ref = {"parent_tool_use_id": <child's parent_tool_call_id>, "request_id": <row request_id>}`. The synthesized events ride the *existing* records-path join (`ref.parent_tool_use_id` → parent ToolCall), so the IR builder and dispatch projection need no new join logic — only a counter (`joined_via_ide_db`), a coverage block (`ide_db`), and a version bump (IR 1.7). Opt-in via `--ide-db` (CLI) / `?ide_db=1` (web). Flag off ⇒ behaviorally inert.

**Tech Stack:** Python 3 stdlib only (`sqlite3` read-only URI, `base64`, `json`, `subprocess` for the openssl fallback). `cryptography` is an optional accelerator, never a dependency (`pyproject.toml` stays `dependencies = []`). Tests: stdlib `unittest`, `PYTHONPATH=src python3 -m unittest`.

---

## Conventions

- Repo root: `/Users/wangting/work/agent-session-detective`. All commands run from here.
- Run tests with `PYTHONPATH=src python3 -m unittest tests.test_<name> -v` (single file) or `PYTHONPATH=src python3 -m pytest -q tests/<file>.py`. The plan's exact commands use unittest.
- Reference spec: `docs/superpowers/specs/2026-10-10-ide-db-subagent-chain-design.md` (committed at `9d4d3e2`, fix `44083da`). The spec is the authority; where this plan deviates it says so explicitly under **Design notes**.
- Staging discipline — shared worktree: stage files by explicit path only, never `git add -A` / `git add .`. No push.
- External-data honesty: the DB is opened `?mode=ro` only; decrypted content stays in-process and is never committed or uploaded.
- No temp files in the repo. Scratch outputs go under `/tmp/` and are deleted after use.

## Design notes (spec deltas an implementer must keep)

1. **IR 1.6 → 1.7** (spec says "bump the IR version"; it did not pin the number in the visible text — this plan pins it at 1.7). Only two schema-visible deltas when the flag is off: the version string and one new empty coverage block `"ide_db": {}`.
2. **Verified live** against session `b1d65022-…` before planning: 9 children (all `session_type='agent_sub_custom'`), 415 rows, `token_info` plaintext on 164/164 assistant rows, `gmt_create` is integer ms epoch, `id` order differs from `gmt_create` order on 9/9 children — hence the SQL orders by `(gmt_create, id)`.
3. **Row shapes** (probe-verified): user rows carry `contents` (list of `{"type": "text", "text": …}`); assistant rows carry `content` (str) + `reasoning_content` (str) + `tool_calls` (list of `{"id", "type", "function": {"name", "arguments"}}` — `arguments` is a JSON **string**, kept verbatim); tool rows carry `content` (str) + `name` + `tool_call_id`, and **no** `is_error` key (default `False`).
4. **Attach point** (locked): `attach_ide_db(session, source, db)` mutates `session.events` and is called after `load_session`, **before** `build_audit_document` — the builder reads events, so anything attached after the build is invisible.
5. **`cli.py` import slot**: `from .ide_db import attach_ide_db` goes between `from .catalog import …` and `from .if_eval import …` (alphabetical: catalog < ide_db < if_eval).
6. **UsageRecord placement**: `_synthesize_child` emits UsageRecord for assistant rows **after** the role if/elif chain, and it is emitted even when the row is undecryptable (the `token_info` column is plaintext — we never lose usable usage data to a decrypt failure). Order per assistant row: text part → think part → ToolCall(s) → UsageRecord.
7. **Decrypt ladder** resolved once per attach: `cryptography` (optional import) → `openssl enc -d -aes-128-cbc` subprocess → honest unavailability. Never raises; per-row failure skips the payload but still counts.
8. **Disk precedence (D6)**: a DB child whose `parent_tool_call_id` is already claimed by a disk-side link (any event `ref.parent_tool_use_id` or any `subagent_meta[*]["toolUseId"]`) is skipped — disk wins, DB only fills gaps.
9. **Exit-code 0 is not acceptance**: the real acceptance is `joined_via_ide_db == 9` and `orphan_dispatches == 0` on the live session (Task 11). The live run must pass `--out /tmp/…` — the default `--out` writes next to the real transcript inside `~/.qoder`.
10. **No `RuntimeConfig`/`ActiveLeaf` synthesis**: model/context window stay unknown for IDE-DB kids (tree badge shows unknown). One assistant DB row = one `WireRecord` = one span = one `LLMCall` — this is the natural granularity, not a bug.
11. **D1: direct children only** — no grandchild recursion (measured: zero grandchildren exist in the corpus).

## File structure

- Create: `src/agent_session_detective/ide_db.py` — decrypt ladder, row→event synthesis, `attach_ide_db`.
- Create: `tests/test_ide_db.py` — all ide_db tests (ladder, synthesis, attach, E2E join).
- Modify: `src/agent_session_detective/ir/schema.py` — `IR_VERSION = "1.7"`, `CoverageReport.ide_db`.
- Modify: `src/agent_session_detective/wire.py` — `Session.ide_db_stats`.
- Modify: `src/agent_session_detective/ir/coverage.py` — `build_coverage(ide_db=…)`.
- Modify: `src/agent_session_detective/ir/builder.py` — pass stats through; `_notes` extension.
- Modify: `src/agent_session_detective/ir/dispatch.py` — `joined_via_ide_db` counter + links key.
- Modify: `src/agent_session_detective/cli.py` — `--ide-db` / `--ide-db-path`, attach call.
- Modify: `src/agent_session_detective/web.py` — `?ide_db=1` on `/api/tree`, `render_tree_page(ide_db=…)`.
- Modify: `src/agent_session_detective/tree_html.py` — "joined via ide-db N" footer segment.
- Modify: `tests/test_ir_schema.py`, `tests/test_wire.py`, `tests/test_ir_builder.py`, `tests/test_ir_dispatch.py`, `tests/test_ir_properties.py`, `tests/test_cli.py`, `tests/test_web.py`, `tests/test_tree_html.py` — pins and new tests.
- Modify: `docs/qoder-transcript-shape.md` — correct the qoder-cli load claim (degraded load, not rejection).

---

### Task 1: Schema + wire fields, IR 1.7 bump, pin updates

**Files:**
- Modify: `src/agent_session_detective/wire.py` (Session dataclass, ~line 103)
- Modify: `src/agent_session_detective/ir/schema.py` (line 15; CoverageReport ~line 180)
- Test: `tests/test_wire.py` (add to `QoderTranscriptTests`), `tests/test_ir_schema.py` (pins at 124/135 + new test), `tests/test_ir_builder.py` (pin 263), `tests/test_ir_dispatch.py` (rename + pins 413/414), `tests/test_cli.py` (pin 184)

- [ ] **Step 1: Write the failing tests**

In `tests/test_wire.py`, add to `QoderTranscriptTests` (anywhere among its test methods):

```python
    def test_session_starts_with_no_ide_db_stats(self):
        self.assertIsNone(self.session.ide_db_stats)
```

In `tests/test_ir_schema.py`, inside `SchemaTest`:

```python
    def test_coverage_gains_an_empty_ide_db_block(self):
        payload = _minimal_document().to_dict()
        self.assertEqual(payload["coverage"]["ide_db"], {})
```

Update the five IR version pins:

- `tests/test_ir_schema.py:124` (`test_constants`): `self.assertEqual(IR_VERSION, "1.7")`
- `tests/test_ir_schema.py:135` (`test_document_round_trips_through_json`): `self.assertEqual(revived["ir_version"], "1.7")`
- `tests/test_ir_builder.py:263`: `self.assertEqual(document.ir_version, "1.7")`
- `tests/test_cli.py:184`: `self.assertEqual(document["ir_version"], "1.7")`
- `tests/test_ir_dispatch.py:412-414`:

```python
    def test_ir_version_is_1_7(self):
        self.assertEqual(IR_VERSION, "1.7")
        self.assertEqual(audit(self).ir_version, "1.7")
```

> Note: `test_ir_schema.py::test_document_top_level_keys_are_exact` enumerates only top-level keys and does **not** enumerate coverage keys — leave it alone. `tests/test_web.py:74-75` "ir1.6" strings are cache fixtures, not pins — leave them alone.

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_schema tests.test_wire -v
```

Expected: FAIL — `IR_VERSION` still `"1.6"`, `Session` has no `ide_db_stats`.

- [ ] **Step 3: Implement**

`src/agent_session_detective/ir/schema.py` line 15:

```python
IR_VERSION = "1.7"
```

In `CoverageReport`, append after the last field (`human_text_evidence: dict = field(default_factory=dict)`):

```python
    # IR 1.7: IDE-DB subagent-chain attach stats (verbatim from the
    # side-channel; {} when the opt-in flag is off).
    ide_db: dict = field(default_factory=dict)
```

`src/agent_session_detective/wire.py` — in `Session`, insert after `subagent_meta: Dict[str, dict] = field(default_factory=dict)` and before `def turns()`:

```python
    # Qoder IDE only: attach stats from the SharedClientCache subagent-chain
    # side-channel (None when the opt-in flag is off).
    ide_db_stats: Optional[dict] = None
```

(`to_dict` is `dataclasses.asdict`, so the JSON shape follows field order — no serializer change.)

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: PASS (full suite green at 1.7).

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/schema.py src/agent_session_detective/wire.py tests/test_ir_schema.py tests/test_wire.py tests/test_ir_builder.py tests/test_ir_dispatch.py tests/test_cli.py
git commit -m "feat: add ide_db_stats/ide_db schema fields and bump the IR to 1.7"
```

---

### Task 2: `ide_db.py` decrypt ladder + row→event synthesis

**Files:**
- Create: `src/agent_session_detective/ide_db.py`
- Test: `tests/test_ide_db.py`

- [ ] **Step 1: Regenerate and verify the test ciphertext constant**

Run this (uses the real ladder; skips silently if no backend):

```bash
python3 - <<'EOF'
import base64, json
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    print("no cryptography; use the openssl fallback manually")
    raise SystemExit
key = b"QbgzpWzN7tfe43gf"
payload = {"role": "assistant", "content": "hello from ide-db",
           "reasoning_content": "", "tool_calls": []}
plain = json.dumps(payload).encode("utf-8")
pad = 16 - len(plain) % 16
padded = plain + bytes([pad]) * pad
enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
blob = enc.update(padded) + enc.finalize()
print(base64.b64encode(blob).decode("ascii"))
EOF
```

Expected output (the constant used below):
`sGRwjAwZdTYr1Gv7UgK2jBJTF5Qf3qjX9F365RIMIwGIn1hofSa419zqZVCDxPu1aiU5JDvdu40VraIvPuY2hpbj1GJcOD+m+5cAW1b1XSeOS/btY9J19TU0OepYCNeIk9x0Hjfse8c6RfTjv1ozDw==`

If the printed value differs, use the printed value in the test below.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_ide_db.py`:

```python
import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import ide_db
from agent_session_detective.wire import Event

KID_UUID = "1b6099f1-205a-4757-8528-d6aa9a1450e2"

PLAINTEXT_ASSISTANT = {"role": "assistant", "content": "hello from ide-db",
                       "reasoning_content": "", "tool_calls": []}
CIPHERTEXT_ASSISTANT = (
    "sGRwjAwZdTYr1Gv7UgK2jBJTF5Qf3qjX9F365RIMIwGIn1hofSa419zqZVCDxPu1"
    "aiU5JDvdu40VraIvPuY2hpbj1GJcOD+m+5cAW1b1XSeOS/btY9J19TU0OepYCNeIk9"
    "x0Hjfse8c6RfTjv1ozDw==")


def identity_decryptor(blob):
    return blob.decode("utf-8")


def coded(payload):
    return base64.b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii")


class DecryptLadderTests(unittest.TestCase):
    def test_identity_seam_decodes_base64_json(self):
        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertEqual(
            ide_db._decrypt_row(raw, identity_decryptor), PLAINTEXT_ASSISTANT)

    def test_non_string_and_empty_rows_decrypt_to_none(self):
        self.assertIsNone(ide_db._decrypt_row(None, identity_decryptor))
        self.assertIsNone(ide_db._decrypt_row("", identity_decryptor))

    def test_bad_base64_decrypts_to_none(self):
        self.assertIsNone(ide_db._decrypt_row("abcde", identity_decryptor))

    def test_bad_json_decrypts_to_none(self):
        raw = base64.b64encode(b"not json").decode("ascii")
        self.assertIsNone(ide_db._decrypt_row(raw, identity_decryptor))

    def test_non_dict_json_decrypts_to_none(self):
        raw = base64.b64encode(b"[1, 2]").decode("ascii")
        self.assertIsNone(ide_db._decrypt_row(raw, identity_decryptor))

    def test_decryptor_raising_value_error_decrypts_to_none(self):
        def boom(blob):
            raise ValueError("bad padding")

        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertIsNone(ide_db._decrypt_row(raw, boom))

    def test_real_ciphertext_decrypts_with_the_resolved_backend(self):
        decrypt = ide_db._resolve_decryptor()
        if decrypt is None:
            self.skipTest("no decryption backend available")
        self.assertEqual(
            ide_db._decrypt_row(CIPHERTEXT_ASSISTANT, decrypt),
            PLAINTEXT_ASSISTANT)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ide_db -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'agent_session_detective.ide_db'`.

- [ ] **Step 4: Implement the module (decrypt ladder + helpers)**

Create `src/agent_session_detective/ide_db.py`:

```python
"""Qoder IDE only: attach subagent chains from the local SharedClientCache DB.

The IDE leaves no subagent transcript on disk — child sessions live in
``chat_session`` (linked to the root by ``parent_tool_call_id``) with the
message payloads AES-encrypted in ``chat_message.content``. This module
decrypts those rows and synthesizes wire events so the existing records-path
join resolves the dispatches the disk side could not.

Opt-in and additive: off by default, never raises, DB opened read-only,
decrypted content stays in-process.
"""

import base64
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import billing
from .wire import Event, Session

_AES_KEY = b"QbgzpWzN7tfe43gf"

_CHILDREN_SQL = """
SELECT session_id, parent_tool_call_id FROM chat_session
WHERE parent_session_id = ? AND parent_tool_call_id != ''
  AND session_type LIKE 'agent_sub%'
ORDER BY session_id
"""

_ROWS_SQL = """
SELECT id, role, content, request_id, token_info, gmt_create
FROM chat_message WHERE session_id = ? ORDER BY gmt_create, id
"""


def _cryptography_decrypt(blob):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_KEY)).decryptor()
    data = decryptor.update(blob) + decryptor.finalize()
    if data:
        pad = data[-1]
        if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
            data = data[:-pad]
    return data


def _openssl_decrypt(blob):
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc",
         "-K", _AES_KEY.hex(), "-iv", _AES_KEY.hex()],
        input=blob, capture_output=True)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def _resolve_decryptor():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        pass
    else:
        return _cryptography_decrypt
    if shutil.which("openssl"):
        return _openssl_decrypt
    return None


def _decrypt_row(raw, decrypt):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        blob = base64.b64decode(raw)
        data = decrypt(blob)
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        payload = json.loads(data)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text_parts(decoded):
    contents = decoded.get("contents")
    if not isinstance(contents, list):
        return []
    return [{"type": "text", "text": part["text"]}
            for part in contents
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)]


def _usage_payload(decoded):
    prompt = decoded.get("prompt_tokens")
    completion = decoded.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    cached = decoded.get("cached_tokens")
    if not isinstance(cached, int):
        cached = 0
    return {"input_other": max(0, prompt - cached),
            "input_cache_read": cached,
            "output": completion}


def _token_usage(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return _usage_payload(payload) if isinstance(payload, dict) else None
```

- [ ] **Step 5: Add the failing synthesis test**

Append to `tests/test_ide_db.py` (before `if __name__ == "__main__":`):

```python
class SynthesizeChildTests(unittest.TestCase):
    def _rows(self, *specs):
        return [tuple(spec) for spec in specs]

    def test_assistant_row_emits_parts_tools_and_usage_in_order(self):
        decoded = {"role": "assistant", "content": "working on it",
                   "reasoning_content": "let me think",
                   "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": "Read",
                                                "arguments": "{\"file_path\": \"/a\"}"}}]}
        rows = self._rows((1, "assistant", coded(decoded), "req-1",
                           json.dumps({"prompt_tokens": 100,
                                       "completion_tokens": 7,
                                       "cached_tokens": 40}), 1754709632658))
        source = Path("/tmp/ide-db-%s.jsonl" % KID_UUID)
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor, source)

        self.assertEqual(failures, 0)
        self.assertEqual([e.type for e in events], [
            "ContentPart", "ContentPart", "ToolCall", "UsageRecord"])
        text, think, call, usage = events
        self.assertEqual(text.payload, {"type": "text", "text": "working on it"})
        self.assertEqual(think.payload, {"type": "think", "think": "let me think"})
        self.assertEqual(call.payload["id"], "call_1")
        self.assertEqual(call.payload["function"]["name"], "Read")
        self.assertEqual(call.payload["function"]["arguments"],
                         "{\"file_path\": \"/a\"}")
        self.assertEqual(usage.payload, {"input_other": 60,
                                         "input_cache_read": 40, "output": 7})
        for event in events:
            self.assertEqual(event.origin, "subagent:ide-db:" + KID_UUID)
            self.assertEqual(event.source, source)
            self.assertEqual(event.seq, 1)
            self.assertEqual(event.ts, 1754709632.658)
            self.assertEqual(event.ref, {"parent_tool_use_id": "call_00A",
                                         "request_id": "req-1"})

    def test_user_row_emits_turn_begin_from_contents(self):
        decoded = {"role": "user",
                   "contents": [{"type": "text", "text": "do the thing"},
                                {"type": "image", "data": "x"}]}
        rows = [(1, "user", coded(decoded), "req-2", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(failures, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "TurnBegin")
        self.assertEqual(events[0].payload["user_input"],
                         [{"type": "text", "text": "do the thing"}])

    def test_user_row_with_no_text_parts_emits_nothing(self):
        decoded = {"role": "user", "contents": [{"type": "image", "data": "x"}]}
        rows = [(1, "user", coded(decoded), "req-2", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual((events, failures), ([], 0))

    def test_tool_row_emits_a_tool_result(self):
        decoded = {"role": "tool", "content": "file contents",
                   "name": "Read", "tool_call_id": "call_1"}
        rows = [(1, "tool", coded(decoded), "req-3", None, 1754709632658)]
        events, _ = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(events[0].type, "ToolResult")
        self.assertEqual(events[0].payload, {
            "tool_call_id": "call_1",
            "return_value": {"output": "file contents", "is_error": False}})

    def test_undecryptable_assistant_row_still_emits_plaintext_usage(self):
        rows = [(1, "assistant", "abcde", "req-1",
                 json.dumps({"prompt_tokens": 10, "completion_tokens": 2,
                             "cached_tokens": 0}), 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(failures, 1)
        self.assertEqual([e.type for e in events], ["UsageRecord"])
        self.assertEqual(events[0].payload, {"input_other": 10,
                                             "input_cache_read": 0, "output": 2})

    def test_empty_content_is_not_counted_as_a_failure(self):
        rows = [(1, "assistant", "", "req-1", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual((events, failures), ([], 0))

    def test_seq_follows_row_order(self):
        decoded = {"role": "user", "contents": [{"type": "text", "text": "hi"}]}
        rows = [(1, "user", coded(decoded), "req-1", None, 1754709632658),
                (2, "user", coded(decoded), "req-2", None, 1754709632659)]
        events, _ = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual([e.seq for e in events], [1, 2])
        self.assertEqual([e.ts for e in events], [1754709632.658, 1754709632.659])
```

- [ ] **Step 6: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ide_db -v
```

Expected: FAIL — `AttributeError: module 'agent_session_detective.ide_db' has no attribute '_synthesize_child'`.

- [ ] **Step 7: Implement `_synthesize_child`**

Append to `src/agent_session_detective/ide_db.py`:

```python
def _synthesize_child(child_id, parent_tool_call_id, rows, decrypt, source):
    events = []
    failures = 0
    origin = "subagent:ide-db:%s" % child_id

    def emit(seq, ts, ref, event_type, payload):
        events.append(Event(ts, event_type, payload, origin, source, seq, ref))

    for seq, row in enumerate(rows, start=1):
        _row_id, role, content, request_id, token_info, gmt_create = row[:6]
        ts = billing._gmt_create_to_ts(gmt_create)
        ref = {"parent_tool_use_id": parent_tool_call_id,
               "request_id": request_id}
        decoded = _decrypt_row(content, decrypt)
        if decoded is None:
            if isinstance(content, str) and content:
                failures += 1
        elif role == "user":
            parts = _text_parts(decoded)
            if parts:
                emit(seq, ts, ref, "TurnBegin", {"user_input": parts})
        elif role == "assistant":
            text = decoded.get("content")
            if isinstance(text, str) and text:
                emit(seq, ts, ref, "ContentPart", {"type": "text", "text": text})
            think = decoded.get("reasoning_content")
            if isinstance(think, str) and think:
                emit(seq, ts, ref, "ContentPart",
                     {"type": "think", "think": think})
            calls = decoded.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    fn = fn if isinstance(fn, dict) else {}
                    emit(seq, ts, ref, "ToolCall", {
                        "id": call.get("id"),
                        "function": {"name": fn.get("name"),
                                     "arguments": fn.get("arguments")}})
        elif role == "tool":
            emit(seq, ts, ref, "ToolResult", {
                "tool_call_id": decoded.get("tool_call_id"),
                "return_value": {
                    "output": decoded.get("content", ""),
                    "is_error": bool(decoded.get("is_error", False)),
                },
            })
        if role == "assistant":
            usage = _token_usage(token_info)
            if usage is not None:
                emit(seq, ts, ref, "UsageRecord", usage)
    return events, failures
```

- [ ] **Step 8: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ide_db -v
```

Expected: PASS (all ladder + synthesis tests; the real-ciphertext test runs or skips).

- [ ] **Step 9: Commit**

```bash
git add src/agent_session_detective/ide_db.py tests/test_ide_db.py
git commit -m "feat: add the ide_db decrypt ladder and row->event synthesis"
```

---

### Task 3: `attach_ide_db` — child queries, disk precedence, staged semantics

**Files:**
- Modify: `src/agent_session_detective/ide_db.py`
- Test: `tests/test_ide_db.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_ide_db.py`, extend the imports to:

```python
import base64
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import ide_db
from agent_session_detective.wire import Event, Session, load_session

from tests.test_ir_dispatch import _clock, assistant_record, user_record

ROOT_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"
KID_UUID = "1b6099f1-205a-4757-8528-d6aa9a1450e2"
KID_UUID_2 = "2c7099f1-205a-4757-8528-d6aa9a1450e3"
FIXTURES = Path(__file__).parent / "fixtures"
```

Append after `SynthesizeChildTests`:

```python
def row(row_id, role, content=None, request_id="req-1",
        token_info=None, gmt_create=1754709632658):
    return (row_id, role, content, request_id, token_info, gmt_create)


def make_db(path, children=(), messages=()):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE chat_session ("
                 "session_id TEXT PRIMARY KEY, parent_session_id TEXT, "
                 "parent_tool_call_id TEXT, session_type TEXT)")
    conn.execute("CREATE TABLE chat_message ("
                 "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                 "content TEXT, request_id TEXT, token_info TEXT, "
                 "gmt_create INTEGER)")
    for kid_id, parent_tool, session_type in children:
        conn.execute("INSERT INTO chat_session VALUES (?, ?, ?, ?)",
                     (kid_id, ROOT_UUID, parent_tool, session_type))
    for session_id, message in messages:
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (message[0], session_id, message[1], message[2],
                      message[3], message[4], message[5]))
    conn.commit()
    conn.close()


def write_main_transcript(path, tool_id="call_00A"):
    clock = _clock()
    records = [
        user_record(clock(), "m1", "kick off"),
        assistant_record(clock(), "m2",
                         tool_use=(tool_id, "Agent", {"subagent_type": "code",
                                                      "description": "spawned worker"}),
                         request_id="req-1", parent="m1"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")


class AttachIdeDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _transcript(self):
        path = self.base / (ROOT_UUID + ".jsonl")
        write_main_transcript(path)
        return path

    def test_non_qoder_source_is_unavailable(self):
        source = self.base / "plain.jsonl"
        session = Session(directory=self.base)
        ide_db.attach_ide_db(session, source)
        self.assertEqual(session.ide_db_stats, {
            "available": False, "reason": "not a qoder session", "db_path": ""})

    def test_missing_db_is_unavailable_with_the_path(self):
        source = self._transcript()
        session = load_session(source)
        before = len(session.events)
        missing = self.base / "absent.db"
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, missing)
        self.assertEqual(session.ide_db_stats, {
            "available": False, "reason": "db not found: %s" % missing,
            "db_path": str(missing)})
        self.assertEqual(len(session.events), before)

    def test_no_backend_is_unavailable(self):
        source = self._transcript()
        db = self.base / "local.db"
        make_db(db)
        session = load_session(source)
        with patch.object(ide_db, "_resolve_decryptor", return_value=None):
            ide_db.attach_ide_db(session, source, db)
        self.assertEqual(session.ide_db_stats, {
            "available": False,
            "reason": "no decryption backend (cryptography or openssl)",
            "db_path": str(db)})

    def test_attaches_children_with_exact_stats(self):
        source = self._transcript()
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hello", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload), "req-1",
                                         json.dumps({"prompt_tokens": 5,
                                                     "completion_tokens": 1,
                                                     "cached_tokens": 0})))])
        session = load_session(source)
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)

        self.assertEqual(session.ide_db_stats, {
            "available": True, "db_path": str(db), "children_found": 1,
            "synthesized": 1, "rows": 1, "decrypt_failures": 0,
            "skipped_already_joined": 0})
        new = session.events[before:]
        self.assertEqual([e.type for e in new], ["ContentPart", "UsageRecord"])
        for event in new:
            self.assertEqual(event.origin, "subagent:ide-db:" + KID_UUID)
            self.assertEqual(event.source,
                             source.parent / ("ide-db-%s.jsonl" % KID_UUID))
            self.assertEqual(event.ref["parent_tool_use_id"], "call_00A")

    def test_children_claimed_by_disk_records_are_skipped(self):
        source = self._transcript()
        session = load_session(source)
        session.events.append(Event(None, "ToolResult", {}, "main", source, 9,
                                    {"parent_tool_use_id": "call_00A"}))
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hello", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)

        self.assertEqual(session.ide_db_stats["skipped_already_joined"], 1)
        self.assertEqual(session.ide_db_stats["synthesized"], 0)
        self.assertEqual(len(session.events), before)

    def test_children_claimed_by_subagent_meta_are_skipped(self):
        source = self._transcript()
        session = load_session(source)
        session.subagent_meta = {"subagent:x": {"toolUseId": "call_00A"}}
        db = self.base / "local.db"
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "user",
                                         coded({"role": "user", "contents": []})))])
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        self.assertEqual(session.ide_db_stats["skipped_already_joined"], 1)

    def test_missing_tables_are_unavailable_not_a_crash(self):
        source = self._transcript()
        db = self.base / "empty.db"
        sqlite3.connect(str(db)).close()
        session = load_session(source)
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        stats = session.ide_db_stats
        self.assertFalse(stats["available"])
        self.assertIn("chat_session", stats["reason"])
        self.assertEqual(len(session.events), before)

    def test_corrupt_db_file_never_raises(self):
        source = self._transcript()
        db = self.base / "broken.db"
        db.write_bytes(b"definitely not a sqlite db")
        session = load_session(source)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        self.assertFalse(session.ide_db_stats["available"])

    def test_mid_query_failure_drops_staged_events_and_stays_unavailable(self):
        source = self._transcript()
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hi", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom"),
                          (KID_UUID_2, "call_00B", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
        session = load_session(source)
        before = len(session.events)
        calls = {"n": 0}
        real_query_rows = ide_db._query_rows

        def flaky(conn, child_id):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("boom")
            return real_query_rows(conn, child_id)

        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor), \
                patch.object(ide_db, "_query_rows", side_effect=flaky):
            ide_db.attach_ide_db(session, source, db)

        stats = session.ide_db_stats
        self.assertFalse(stats["available"])
        self.assertIn("boom", stats["reason"])
        self.assertEqual(len(session.events), before)
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ide_db -v
```

Expected: FAIL — `AttributeError: module 'agent_session_detective.ide_db' has no attribute 'attach_ide_db'`.

- [ ] **Step 3: Implement**

Append to `src/agent_session_detective/ide_db.py`:

```python
def _query_children(conn, parent_session_id):
    return conn.execute(_CHILDREN_SQL, (parent_session_id,)).fetchall()


def _query_rows(conn, child_id):
    return conn.execute(_ROWS_SQL, (child_id,)).fetchall()


def _claimed_tool_ids(session):
    claimed = set()
    for event in session.events:
        ref = event.ref or {}
        tool_use_id = ref.get("parent_tool_use_id")
        if tool_use_id:
            claimed.add(tool_use_id)
    for meta in session.subagent_meta.values():
        tool_use_id = meta.get("toolUseId")
        if tool_use_id:
            claimed.add(tool_use_id)
    return claimed


def _unavailable(reason, path=None):
    return {"available": False, "reason": reason,
            "db_path": str(path) if path is not None else ""}


def attach_ide_db(session, source_path, db_path=None):
    """Attach IDE DB subagent chains to ``session`` (opt-in, never raises).

    Reads direct child sessions of the root session from the SharedClientCache
    ``local.db`` and appends synthesized events to ``session.events``. On any
    failure the session is left untouched and ``session.ide_db_stats`` records
    why (unavailable). Returns None; the caller reads ``session.ide_db_stats``.
    """
    session_uuid = billing.session_uuid_from_source(source_path)
    if session_uuid is None:
        session.ide_db_stats = _unavailable("not a qoder session")
        return
    path = Path(db_path if db_path is not None else billing.DEFAULT_DB_PATH)
    if not path.is_file():
        session.ide_db_stats = _unavailable("db not found: %s" % path, path)
        return
    decrypt = _resolve_decryptor()
    if decrypt is None:
        session.ide_db_stats = _unavailable(
            "no decryption backend (cryptography or openssl)", path)
        return

    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        children = _query_children(conn, session_uuid)
        claimed = _claimed_tool_ids(session)
        staged = []
        decrypt_failures = 0
        skipped = 0
        rows_read = 0
        synthesized = 0
        for child_id, parent_tool_call_id in children:
            if parent_tool_call_id in claimed:
                skipped += 1
                continue
            child_rows = _query_rows(conn, child_id)
            source = session.directory / ("ide-db-%s.jsonl" % child_id)
            events, failures = _synthesize_child(
                child_id, parent_tool_call_id, child_rows, decrypt, source)
            decrypt_failures += failures
            rows_read += len(child_rows)
            synthesized += 1
            staged.extend(events)
    except sqlite3.Error as exc:
        session.ide_db_stats = _unavailable(str(exc), path)
        return
    finally:
        if conn is not None:
            conn.close()

    session.events.extend(staged)
    session.ide_db_stats = {
        "available": True,
        "db_path": str(path),
        "children_found": len(children),
        "synthesized": synthesized,
        "rows": rows_read,
        "decrypt_failures": decrypt_failures,
        "skipped_already_joined": skipped,
    }
```

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ide_db -v
```

Expected: PASS (all attach tests).

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ide_db.py tests/test_ide_db.py
git commit -m "feat: add attach_ide_db with child queries and disk precedence"
```

---

### Task 4: Thread the `ide_db` block through `build_coverage`

**Files:**
- Modify: `src/agent_session_detective/ir/coverage.py` (signature ~103-118, construction ~151)
- Modify: `src/agent_session_detective/ir/builder.py` (build_coverage call ~343-357)
- Test: `tests/test_ir_builder.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ir_builder.py` (new class at end of file):

```python
class IdeDbLandingTests(unittest.TestCase):
    def test_session_ide_db_stats_land_in_the_coverage_block(self):
        session = load_session(FIXTURES / "tier1.jsonl")
        session.ide_db_stats = {
            "available": True, "db_path": "/tmp/local.db", "children_found": 2,
            "synthesized": 2, "rows": 40, "decrypt_failures": 0,
            "skipped_already_joined": 0}
        document = build_audit_document(session, "qoder")
        self.assertEqual(document.coverage.ide_db, session.ide_db_stats)
```

(Reuse the file's existing `load_session`, `build_audit_document`, `FIXTURES` imports; if `build_audit_document` is not imported directly at top of file, mirror how `build()` calls it.)

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_builder -v
```

Expected: FAIL — `document.coverage.ide_db` is `{}`.

- [ ] **Step 3: Implement**

`src/agent_session_detective/ir/coverage.py` — `build_coverage` signature gains `ide_db: Optional[dict] = None,` after `skill_identity: Optional[dict] = None,`; and the `CoverageReport(...)` construction gains `ide_db=ide_db or {},` immediately before `intervention_evidence=_intervention_evidence(extraction.interventions),`.

`src/agent_session_detective/ir/builder.py` — in the `build_coverage(...)` call, add `ide_db=session.ide_db_stats,` after `skill_identity=skill_identity,`. (Do not touch `notes=_notes(...)` here — that call-site change belongs solely to Task 6.)

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_builder -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/coverage.py src/agent_session_detective/ir/builder.py tests/test_ir_builder.py
git commit -m "feat: thread the ide_db block through build_coverage"
```

---

### Task 5: Count ide-db joins in the dispatch links

**Files:**
- Modify: `src/agent_session_detective/ir/dispatch.py` (~230-232 counters, ~245-249 branch, ~369-378 links)
- Test: `tests/test_ir_dispatch.py` (links dict assertion ~202-212), `tests/test_ir_properties.py` (invariant ~347-352)

- [ ] **Step 1: Write the failing test changes**

`tests/test_ir_dispatch.py` — extend the exact-dict assertion:

```python
        links = document.coverage.dispatch_links
        self.assertEqual(links, {
            "dispatches": 3,
            "joined": 1,
            "joined_via_ide_db": 0,
            "joined_via_meta_only": 1,
            "orphan_dispatches": 1,
            "unmatched_subagent_files": 1,
            "meta_files_loaded": 1,
            "briefs_found": 2,
            "briefs_missing": 1,
        })
```

`tests/test_ir_properties.py` — extend the reconciliation invariant:

```python
            self.assertEqual(
                links["joined"] + links["joined_via_meta_only"]
                + links["joined_via_ide_db"] + links["orphan_dispatches"],
                len(dispatches),
                name,
            )
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_dispatch tests.test_ir_properties -v
```

Expected: FAIL — links dict missing `joined_via_ide_db` (the properties test fails with `KeyError: 'joined_via_ide_db'`).

- [ ] **Step 3: Implement**

`src/agent_session_detective/ir/dispatch.py` — add `joined_via_ide_db = 0` beside the existing counters (`joined = 0` / `joined_via_meta_only = 0` / `orphan_dispatches = 0`), and change the counting branch:

```python
            if subagent_agent_id is not None:
                if row["link_source"] == "records":
                    if subagent_agent_id.startswith("subagent:ide-db:"):
                        joined_via_ide_db += 1
                    else:
                        joined += 1
                else:
                    joined_via_meta_only += 1
```

In the `dispatch_links` dict, insert after `"joined": joined,`:

```python
        "joined_via_ide_db": joined_via_ide_db,
```

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_dispatch tests.test_ir_properties -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/dispatch.py tests/test_ir_dispatch.py tests/test_ir_properties.py
git commit -m "feat: count ide-db joined dispatches in the dispatch links"
```

---

### Task 6: Coverage note + `_notes` signature + end-to-end join test

**Files:**
- Modify: `src/agent_session_detective/ir/builder.py` (`_notes` ~233-253, call site)
- Test: `tests/test_ir_builder.py` (note tests), `tests/test_ide_db.py` (E2E)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ir_builder.py`:

```python
class IdeDbNoteTests(unittest.TestCase):
    def test_attached_stats_produce_one_summary_note(self):
        from agent_session_detective.ir.builder import _notes

        stats = {"available": True, "db_path": "/tmp/local.db",
                 "children_found": 9, "synthesized": 9, "rows": 415,
                 "decrypt_failures": 0, "skipped_already_joined": 0}
        notes = _notes("qoder", {}, stats)
        self.assertEqual(
            notes[-1],
            "ide_db: attached (children=9 synthesized=9 rows=415 "
            "decrypt_failures=0 skipped_already_joined=0)")

    def test_unavailable_stats_produce_a_reason_note(self):
        from agent_session_detective.ir.builder import _notes

        notes = _notes("qoder", {}, {"available": False,
                                     "reason": "not a qoder session",
                                     "db_path": ""})
        self.assertEqual(notes[-1], "ide_db: unavailable (not a qoder session)")

    def test_no_stats_means_no_ide_db_note(self):
        from agent_session_detective.ir.builder import _notes

        notes = _notes("qoder", {}, None)
        self.assertFalse(any(n.startswith("ide_db:") for n in notes))
```

Append to `tests/test_ide_db.py`:

```python
class EndToEndJoinTests(unittest.TestCase):
    def test_ide_db_child_joins_the_dispatch_and_unorphans_it(self):
        from agent_session_detective.cli import _detect_adapter
        from agent_session_detective.ir import build_audit_document

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            db = base / "local.db"
            user = {"role": "user",
                    "contents": [{"type": "text", "text": "investigate the bug"}]}
            assistant = {"role": "assistant", "content": "on it",
                         "reasoning_content": "", "tool_calls": []}
            make_db(db,
                    children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                    messages=[
                        (KID_UUID, row(1, "user", coded(user), "req-1")),
                        (KID_UUID, row(2, "assistant", coded(assistant), "req-1",
                                      json.dumps({"prompt_tokens": 100,
                                                  "completion_tokens": 5,
                                                  "cached_tokens": 60})))])
            session = load_session(transcript)
            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=identity_decryptor):
                ide_db.attach_ide_db(session, transcript, db)
            document = build_audit_document(
                session, _detect_adapter(session, transcript))

        links = document.coverage.dispatch_links
        self.assertEqual(links["dispatches"], 1)
        self.assertEqual(links["joined_via_ide_db"], 1)
        self.assertEqual(links["joined"], 0)
        self.assertEqual(links["orphan_dispatches"], 0)
        dispatch = document.dispatches[0]
        self.assertEqual(dispatch.subagent_agent_id,
                         "subagent:ide-db:" + KID_UUID)
        self.assertTrue(document.coverage.notes[-1].startswith("ide_db: attached"))

    def test_flag_off_leaves_the_document_inert(self):
        from agent_session_detective.cli import _detect_adapter
        from agent_session_detective.ir import build_audit_document

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            session = load_session(transcript)
            document = build_audit_document(
                session, _detect_adapter(session, transcript))

        self.assertIsNone(session.ide_db_stats)
        self.assertEqual(document.coverage.ide_db, {})
        links = document.coverage.dispatch_links
        self.assertEqual(links["dispatches"], 1)
        self.assertEqual(links["orphan_dispatches"], 1)
        self.assertFalse(any(n.startswith("ide_db:")
                             for n in document.coverage.notes))
```

(These tests need `KID_UUID == "1b6099f1-205a-4757-8528-d6aa9a1450e2"` — this is what makes the join work: `subagent:ide-db:<uuid>` matches `_is_subagent` prefix rules and the Agent tool call's `parent` grouping.)

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_builder.IdeDbNoteTests -v
```

Expected: FAIL — `_notes()` takes 2 positional args.

- [ ] **Step 3: Implement**

`src/agent_session_detective/ir/builder.py` — `_notes` signature becomes `def _notes(adapter_id, tiers, ide_db_stats=None):`. Append inside, after the tier-3 loop:

```python
    if ide_db_stats is not None:
        if ide_db_stats.get("available"):
            notes.append(
                "ide_db: attached (children=%d synthesized=%d rows=%d "
                "decrypt_failures=%d skipped_already_joined=%d)" % (
                    ide_db_stats["children_found"], ide_db_stats["synthesized"],
                    ide_db_stats["rows"], ide_db_stats["decrypt_failures"],
                    ide_db_stats["skipped_already_joined"]))
        else:
            notes.append("ide_db: unavailable (%s)"
                         % ide_db_stats.get("reason"))
```

Call site: change `notes=_notes(adapter_id, tiers),` to `notes=_notes(adapter_id, tiers, session.ide_db_stats),`.

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_ir_builder tests.test_ide_db -v
```

Expected: PASS — including the E2E join showing `joined_via_ide_db == 1` / `orphan_dispatches == 0`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/ir/builder.py tests/test_ir_builder.py tests/test_ide_db.py
git commit -m "feat: attach the ide_db summary note and prove the E2E join"
```

---

### Task 7: CLI flags and wiring

**Files:**
- Modify: `src/agent_session_detective/cli.py` (imports ~12-31, flags ~67-73, flow ~126-131)
- Test: `tests/test_cli.py` (new `IdeDbFlagTests`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (mirroring `BilledUsageFlagTests`; `BILLING_UUID` already exists at ~line 221):

```python
class IdeDbFlagTests(unittest.TestCase):
    def _write_fixture(self, base):
        transcript = base / (BILLING_UUID + ".jsonl")
        shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
        return transcript

    def test_ide_db_flag_attaches_the_chain_from_the_given_db(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = self._write_fixture(base)
            db = base / "local.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE chat_session ("
                         "session_id TEXT PRIMARY KEY, parent_session_id TEXT, "
                         "parent_tool_call_id TEXT, session_type TEXT)")
            conn.execute("CREATE TABLE chat_message ("
                         "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                         "content TEXT, request_id TEXT, token_info TEXT, "
                         "gmt_create INTEGER)")
            conn.commit()
            conn.close()

            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=lambda blob: blob.decode("utf-8")), \
                    patch("agent_session_detective.cli.render_report",
                          return_value="<html>report</html>") as render:
                code = cli.main([str(transcript), "--no-judge", "--ide-db",
                                 "--ide-db-path", str(db),
                                 "--ir-out", str(base / "ir.json")])

            self.assertEqual(code, 0)
            document = render.call_args.kwargs["document"]
            self.assertEqual(document.coverage.ide_db, {
                "available": True, "db_path": str(db), "children_found": 0,
                "synthesized": 0, "rows": 0, "decrypt_failures": 0,
                "skipped_already_joined": 0})
            self.assertTrue(document.coverage.notes[-1].startswith(
                "ide_db: attached"))

    def test_ide_db_flag_with_a_missing_default_db_notes_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = self._write_fixture(base)
            with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                       str(base / "absent.db")), \
                    patch("agent_session_detective.cli.render_report",
                          return_value="<html>report</html>") as render:
                code = cli.main([str(transcript), "--no-judge", "--ide-db",
                                 "--ir-out", str(base / "ir.json")])

            self.assertEqual(code, 0)
            document = render.call_args.kwargs["document"]
            self.assertFalse(document.coverage.ide_db["available"])
            self.assertIn("db not found", document.coverage.ide_db["reason"])
            self.assertTrue(any(n.startswith("ide_db: unavailable")
                                for n in document.coverage.notes))

    def test_without_the_flag_nothing_attaches(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = self._write_fixture(base)
            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                code = cli.main([str(transcript), "--no-judge",
                                 "--ir-out", str(base / "ir.json")])

            self.assertEqual(code, 0)
            document = render.call_args.kwargs["document"]
            self.assertEqual(document.coverage.ide_db, {})
            self.assertFalse(any(n.startswith("ide_db:")
                                 for n in document.coverage.notes))
```

Add `from agent_session_detective import ide_db` to the top imports of `tests/test_cli.py` (the billing tests already import `sqlite3`, `patch`, `shutil`, `tempfile`, `Path`, `FIXTURES`, and `cli`).

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_cli.IdeDbFlagTests -v
```

Expected: FAIL — `--ide-db` is not a recognized argument (`SystemExit`/code 2).

- [ ] **Step 3: Implement**

`src/agent_session_detective/cli.py` — add the import between `from .catalog import …` and `from .if_eval import …`:

```python
from .ide_db import attach_ide_db
```

Add the flag pair after `--billed-db`:

```python
    parser.add_argument("--ide-db", action="store_true",
                        help="Attach IDE-channel subagent chains from the local "
                             "SharedClientCache DB (opt-in; Qoder transcripts only).")
    parser.add_argument("--ide-db-path", default=None, metavar="PATH",
                        help="IDE subagent DB path (default: Qoder SharedClientCache "
                             "local.db).")
```

Insert the attach between the empty-events guard and the build:

```python
    if args.ide_db:
        attach_ide_db(session, session_source, args.ide_db_path)
```

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_cli -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/cli.py tests/test_cli.py
git commit -m "feat: add --ide-db/--ide-db-path to the CLI"
```

---

### Task 8: Footer segment "joined via ide-db N"

**Files:**
- Modify: `src/agent_session_detective/tree_html.py` (`_footer` ~98-102)
- Test: `tests/test_tree_html.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tree_html.py` (`render_skill_tree` is imported directly; `agent`/`CHILD`/`MAIN`/`dispatch`/`document` come from `tests.test_skill_tree`; the local `page(*args, **kwargs)` helper is `render_skill_tree(document(*args, **kwargs))`):

```python
class IdeDbFooterTests(unittest.TestCase):
    def test_zero_joins_keep_the_footer_without_the_segment(self):
        out = page(agents=[agent(MAIN)])
        self.assertNotIn("joined via ide-db", out)

    def test_positive_joins_render_the_segment(self):
        doc = document([agent(MAIN)],
                       dispatches=[dispatch("main:dispatch:1", MAIN, CHILD, "code")])
        doc.coverage.dispatch_links["joined_via_ide_db"] = 2
        out = render_skill_tree(doc)
        self.assertIn("joined via ide-db 2", out)
```

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_tree_html -v
```

Expected: FAIL — `joined via ide-db` not rendered.

- [ ] **Step 3: Implement**

`src/agent_session_detective/tree_html.py` — replace the dispatches segment (keep byte-identical output when the counter is 0):

```python
    text += (" · dispatches %d / joined via records %d%s / joined via meta.json "
             "only %d / orphan dispatches %d") % (
                 links.get("dispatches", 0), links.get("joined", 0),
                 (" / joined via ide-db %d" % links["joined_via_ide_db"]
                  if links.get("joined_via_ide_db", 0) else ""),
                 links.get("joined_via_meta_only", 0),
                 links.get("orphan_dispatches", 0))
```

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_tree_html -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/tree_html.py tests/test_tree_html.py
git commit -m "feat: render the joined-via-ide-db footer segment"
```

---

### Task 9: Web route `?ide_db=1` + `render_tree_page(ide_db=…)`

**Files:**
- Modify: `src/agent_session_detective/web.py` (`render_tree_page` 587-606, route 799-814)
- Test: `tests/test_web.py` (new test in `TreeRouteTests`)

- [ ] **Step 1: Write the failing test**

In `tests/test_web.py`, inside `TreeRouteTests` (mirroring the billed test's direct `billing.DEFAULT_DB_PATH` assignment — the module attribute is read at call time). This test builds a real join through the HTTP route: the transcript carries an `Agent` tool call `call_00A`, the DB child rides `parent_tool_call_id = "call_00A"`, and the assertion reads the visible footer segment landed in Task 8:

```python
    def test_ide_db_param_joins_the_chain_from_the_default_db(self):
        import agent_session_detective.billing as billing
        from agent_session_detective import ide_db
        from tests.test_ide_db import (KID_UUID, ROOT_UUID, coded,
                                       identity_decryptor, make_db, row,
                                       write_main_transcript)

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (ROOT_UUID + ".jsonl")
            write_main_transcript(transcript)
            db = base / "local.db"
            payload = {"role": "assistant", "content": "hello",
                       "reasoning_content": "", "tool_calls": []}
            make_db(db,
                    children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                    messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = db
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)
            with patch.object(ide_db, "_resolve_decryptor",
                              return_value=identity_decryptor):
                with urllib.request.urlopen(
                        self.tree_url(str(transcript)) + "&ide_db=1") as response:
                    body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("joined via ide-db 1", body)
```

(`tree_url`, `sqlite3`, `patch`, `urllib` already exist in this file; `tests.test_ide_db` supplies the fixture builders. `write_main_transcript` writes the transcript under `ROOT_UUID` — the filename `session_uuid_from_source` reads.)

- [ ] **Step 2: Run to verify failure**

```bash
PYTHONPATH=src python3 -m unittest tests.test_web.TreeRouteTests -v
```

Expected: FAIL — page has no "joined via ide-db" segment (route param not wired) or 500 if wiring is missing.

- [ ] **Step 3: Implement**

`src/agent_session_detective/web.py` — `render_tree_page` signature becomes `def render_tree_page(session_path: str, billed: bool = False, ide_db: bool = False) -> str:`; insert after the `if not session.events:` guard and before the build:

```python
    if ide_db:
        from .ide_db import attach_ide_db
        attach_ide_db(session, source_path)
```

Route: extend the `render_tree_page(...)` call:

```python
                page = render_tree_page(
                    path, billed=(params.get("billed") == ["1"]),
                    ide_db=(params.get("ide_db") == ["1"]))
```

- [ ] **Step 4: Run to verify pass**

```bash
PYTHONPATH=src python3 -m unittest tests.test_web -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: serve the ide-db chain on GET /api/tree?ide_db=1"
```

---

### Task 10: Correct the qoder-cli load claim in the shape doc

**Files:**
- Modify: `docs/qoder-transcript-shape.md` (~85-88)

- [ ] **Step 1: Verify the actual behavior (no code change)**

```bash
PYTHONPATH=src python3 - <<'EOF'
from pathlib import Path
from agent_session_detective.wire import load_session
import glob
paths = glob.glob(str(Path("~/.qoder/projects").expanduser() / "**" / "*.jsonl"),
                  recursive=True)
for p in paths:
    try:
        session = load_session(Path(p))
        print(p.split("/")[-1], session.source_format,
              session.compaction_telemetry_available)
        break
    except ValueError:
        continue
EOF
```

Expected: a line ending in `qoder-cli False` (degraded load, no rejection). If the local corpus has no qoder-cli sessions, rely on the test suite: `tests/test_cli.py`/`tests/test_web.py` cover the degraded path.

- [ ] **Step 2: Edit the doc**

In `docs/qoder-transcript-shape.md` lines ~85-88, replace the claim that `load_session` explicitly rejects qoder-cli sessions with `ValueError` with the actual behavior: qoder-cli sessions **load degraded** — `source_format="qoder-cli"`, `compaction_telemetry_available=False` — and pages/trees render with a "no usage telemetry" notice; there is no rejection.

- [ ] **Step 3: Commit**

```bash
git add docs/qoder-transcript-shape.md
git commit -m "docs: correct the qoder-cli load claim in the transcript shape doc"
```

---

### Task 11: Full suite + live acceptance on the real session (no commit)

**Files:** none (verification only).

- [ ] **Step 1: Full suite**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: PASS, zero failures.

- [ ] **Step 2: Live acceptance on `b1d65022-…`**

```bash
PYTHONPATH=src python3 -m agent_session_detective \
  "/Users/wangting/.qoder/projects/-Users-wangting-work-paze-test/transcript/b1d65022-d35d-4a45-b24f-27eb970e6b86.jsonl" \
  --ide-db --no-judge \
  --ir-out /tmp/ide-db-acceptance.json \
  --out /tmp/ide-db-acceptance.html
```

Expected: exit 0. Then:

```bash
PYTHONPATH=src python3 - <<'EOF'
import json
doc = json.load(open("/tmp/ide-db-acceptance.json"))
coverage = doc["coverage"]
print("joined_via_ide_db:", coverage["dispatch_links"]["joined_via_ide_db"])
print("orphan_dispatches:", coverage["dispatch_links"]["orphan_dispatches"])
print("ide_db:", coverage["ide_db"])
assert coverage["dispatch_links"]["joined_via_ide_db"] == 9, "expected 9"
assert coverage["dispatch_links"]["orphan_dispatches"] == 0, "expected 0"
assert coverage["ide_db"]["available"] is True
assert coverage["ide_db"]["children_found"] == 9
assert coverage["ide_db"]["synthesized"] == 9
print("LIVE ACCEPTANCE OK")
EOF
```

Expected: `LIVE ACCEPTANCE OK`.

- [ ] **Step 3: Idempotence check — flag off is inert**

```bash
PYTHONPATH=src python3 -m agent_session_detective \
  "/Users/wangting/.qoder/projects/-Users-wangting-work-paze-test/transcript/b1d65022-d35d-4a45-b24f-27eb970e6b86.jsonl" \
  --no-judge --ir-out /tmp/ide-db-off.json
```

Then diff the coverage blocks: the flag-off document must have `"ide_db": {}`, `"joined_via_ide_db": 0`, the pre-existing orphan count, and no `ide_db:` note. (Other fields should agree; if the session changed on disk since the probe, note the drift rather than failing.)

- [ ] **Step 4: Clean up**

```bash
rm -f /tmp/ide-db-acceptance.json /tmp/ide-db-acceptance.html /tmp/ide-db-off.json
```

Do not commit anything in this task — acceptance artifacts are scratch. No files were modified under `~/.qoder` (the `--out /tmp/…` flag prevents that).
