# Billed-Usage Side-Channel (IR first-class, opt-in) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show provider-billed token totals (from Qoder's local SharedClientCache SQLite `chat_message.token_info`) alongside the EST estimates on the skill-tree surface, opt-in via an explicit flag, with provenance declared; EST and billed lenses never mixed.

**Architecture:** The builder is untouched. `BilledUsage` becomes an optional first-class IR field (`AuditDocument.billing`), attached post-build by `billing.attach_billed_usage()` only on explicit opt-in. Every failure is counted and reasoned (one `coverage.notes` line), never guessed; a billed number renders only when ≥1 parseable `token_info` row exists for the session uuid. `DEFAULT_DB_PATH` is resolved inside the function body (module-global lookup), never as a default-arg default, so tests can patch it.

**Tech Stack:** Pure Python stdlib (sqlite3, json, dataclasses, re, unittest under pytest). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-09-billed-usage-ir-design.md` (committed as 4ca6f93)

**Shared-worktree discipline (applies to every task):** stage only the files listed in the task's **Files** block, by explicit path. Never `git add -A` / `git add .`. Never push. Never touch `docs/qoder-transcript-shape.md`, `src/agent_session_detective/ir/intervention.py`, `tests/test_ir_intervention.py` (foreign files). Never read or write the real user DB from tests — synthetic tmp fixtures only.

---

## Task 0: Concurrency gate (run FIRST, before Task 1)

- [ ] **Step 0.1: verify no tracked file is modified**

```bash
git status --porcelain
```

Expected: no tracked-file modification lines (no ` M`/`M `/`MM` entries). The foreign untracked files `docs/qoder-transcript-shape.md`, `src/agent_session_detective/ir/intervention.py`, `tests/test_ir_intervention.py` may be present — leave them alone. If ANY tracked file is modified: **STOP. Report the status output and wait for the user. Do not implement.**

- [ ] **Step 0.2: pin the IR_VERSION bump target**

```bash
git show HEAD:src/agent_session_detective/ir/schema.py | grep -m1 '^IR_VERSION'
```

If it prints `IR_VERSION = "1.3"` → Task 1 sets `IR_VERSION = "1.4"`. If it prints `IR_VERSION = "1.2"` → Task 1 sets `IR_VERSION = "1.3"`. Record the target; the tests never hardcode it.

- [ ] **Step 0.3: green baseline**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all tests pass. If the baseline is red: **STOP. Report and wait. Do not fix foreign failures.**

---

## Task 1: BilledUsage dataclass + billing field + IR_VERSION pin

**Files:**
- Modify: `src/agent_session_detective/ir/schema.py`
- Modify: `tests/test_ir_schema.py` (import block + appended class at end)

- [ ] **Step 1.1: write the failing tests**

In `tests/test_ir_schema.py`, extend the existing schema import with `BilledUsage` (add one line to the parenthesized import list, alphabetical: after `BUCKETS,`):

```python
from agent_session_detective.ir.schema import (
    BUCKETS,
    BilledUsage,
    IR_VERSION,
    UNATTRIBUTED,
    AuditDocument,
    ContentItem,
    ContextAgent,
    CoverageReport,
    ItemRef,
    LLMCall,
    Observation,
    RequestInput,
    RequestOutput,
    SkillEntity,
    Span,
)
```

Append at the end of the file:

```python
class BilledUsageTest(unittest.TestCase):
    def _document(self):
        return AuditDocument(
            adapter={"id": "test", "version": "0"},
            estimator_version="test",
            source_files=[],
            agents=[],
            requests=[],
            items=[],
            skills=[],
            compactions=[],
            coverage=CoverageReport(
                per_field={}, requests_with_anchor="", request_identity={},
                bucket_sources={}, unknown_channels=[], dropped_records={},
                notes=[]),
        )

    def _billed(self, **overrides):
        fields = dict(
            session_id="3b241101-e2bb-4255-8caf-4136c566a962",
            source="SharedClientCache chat_message.token_info",
            db_path="/tmp/local.db",
            requests=2,
            prompt_tokens=100,
            completion_tokens=10,
            cached_tokens=80,
            rows_total=3,
            rows_malformed=1,
        )
        fields.update(overrides)
        return BilledUsage(**fields)

    def test_billing_field_defaults_to_none(self):
        self.assertIsNone(self._document().billing)

    def test_billing_serializes_before_ir_version(self):
        document = self._document()
        document.billing = self._billed()
        payload = document.to_dict()
        keys = list(payload.keys())
        self.assertLess(keys.index("billing"), keys.index("ir_version"))
        self.assertEqual(payload["billing"]["prompt_tokens"], 100)
        self.assertEqual(payload["billing"]["rows_malformed"], 1)
```

- [ ] **Step 1.2: run to verify RED**

```bash
PYTHONPATH=src python3 -m pytest -q tests/test_ir_schema.py -k BilledUsage
```

Expected: FAIL/ERROR with `ImportError: cannot import name 'BilledUsage'`.

- [ ] **Step 1.3: implement**

In `src/agent_session_detective/ir/schema.py`:

Change the `IR_VERSION` line to the pinned target from Step 0.2 (example shows the expected post-gate case `"1.3"` → `"1.4"`):

```python
IR_VERSION = "1.4"
```

Insert after the `PhaseEntity` dataclass and before `AuditDocument`:

```python
@dataclass
class BilledUsage:
    """Provider-billed token totals (opt-in side-channel, attached post-build).

    Sourced from Qoder's local SharedClientCache DB. EST (per-skill
    attribution) and billed (session totals) are separate lenses: every
    number here is a provider-billed fact; when nothing parseable was
    observed the field stays null and a coverage note explains why.
    """

    session_id: str        # transcript uuid used for the SharedClientCache join
    source: str            # provenance, e.g. "SharedClientCache chat_message.token_info"
    db_path: str           # the DB file actually queried
    requests: int          # parseable token_info rows
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    rows_total: int        # all chat_message rows seen for the session
    rows_malformed: int    # rows skipped (missing/unparseable token_info)
```

In `AuditDocument`, insert one field between `phases` and `ir_version`:

```python
    phases: List[PhaseEntity] = field(default_factory=list)
    # Opt-in side-channel: attached post-build, never produced by the
    # builder; null = not requested, or unavailable (see coverage.notes).
    billing: Optional[BilledUsage] = None
    ir_version: str = IR_VERSION
```

- [ ] **Step 1.4: run to verify GREEN + no regressions**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 1.5: commit**

```bash
git add src/agent_session_detective/ir/schema.py tests/test_ir_schema.py
git commit -m "feat: add BilledUsage to the IR schema as an optional post-build field"
```

---

## Task 2: billing.py — uuid derivation + read-only query

**Files:**
- Create: `src/agent_session_detective/billing.py`
- Test: `tests/test_billing.py` (new file)

- [ ] **Step 2.1: write the failing tests (new file `tests/test_billing.py`)**

```python
"""Billed-usage side-channel tests: uuid join, read-only query, attach."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.billing import (
    BillingUnavailable,
    attach_billed_usage,
    query_billed_usage,
    session_uuid_from_source,
)
from agent_session_detective.ir.schema import AuditDocument, CoverageReport

UUID = "3b241101-e2bb-4255-8caf-4136c566a962"


def make_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
    for session_id, token_info in rows:
        conn.execute("INSERT INTO chat_message VALUES (?, ?)",
                     (session_id, token_info))
    conn.commit()
    conn.close()
    return path


def make_document() -> AuditDocument:
    return AuditDocument(
        adapter={"id": "test", "version": "0"},
        estimator_version="test",
        source_files=[],
        agents=[],
        requests=[],
        items=[],
        skills=[],
        compactions=[],
        coverage=CoverageReport(
            per_field={}, requests_with_anchor="", request_identity={},
            bucket_sources={}, unknown_channels=[], dropped_records={},
            notes=[]),
    )


class SessionUuidTest(unittest.TestCase):
    def test_uuid_shaped_transcript_stem_is_the_session_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(session_uuid_from_source(source), UUID)

    def test_uppercase_uuid_stem_normalizes_to_lower(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (UUID.upper() + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(session_uuid_from_source(source), UUID.lower())

    def test_non_uuid_name_is_not_a_session(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tier1.jsonl"
            source.write_text("{}", encoding="utf-8")
            self.assertIsNone(session_uuid_from_source(source))

    def test_directory_source_is_not_a_session(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(session_uuid_from_source(Path(directory)))


class QueryTest(unittest.TestCase):
    def test_aggregates_token_info_rows_for_the_session_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80,
                                   "max_input_tokens": 180000})),
                (UUID, json.dumps({"prompt_tokens": 50, "completion_tokens": 7,
                                   "cached_tokens": 20})),
                ("other-session", json.dumps({"prompt_tokens": 999,
                                              "completion_tokens": 1,
                                              "cached_tokens": 0})),
            ])
            billed = query_billed_usage(UUID, db_path=db)
            self.assertEqual(billed.session_id, UUID)
            self.assertEqual(billed.requests, 2)
            self.assertEqual(billed.prompt_tokens, 150)
            self.assertEqual(billed.completion_tokens, 12)
            self.assertEqual(billed.cached_tokens, 100)
            self.assertEqual(billed.rows_total, 2)
            self.assertEqual(billed.rows_malformed, 0)
            self.assertEqual(billed.source, "SharedClientCache chat_message.token_info")
            self.assertEqual(billed.db_path, str(db))

    def test_malformed_rows_are_counted_never_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80})),
                (UUID, json.dumps({"model_key": "auto"})),
                (UUID, "not-json"),
            ])
            billed = query_billed_usage(UUID, db_path=db)
            self.assertEqual(billed.requests, 1)
            self.assertEqual(billed.prompt_tokens, 100)
            self.assertEqual(billed.rows_total, 3)
            self.assertEqual(billed.rows_malformed, 2)

    def test_zero_rows_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [])
            with self.assertRaises(BillingUnavailable) as ctx:
                query_billed_usage(UUID, db_path=db)
            self.assertIn("no billed rows", ctx.exception.reason)

    def test_all_rows_malformed_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"model_key": "auto"})),
            ])
            with self.assertRaises(BillingUnavailable) as ctx:
                query_billed_usage(UUID, db_path=db)
            self.assertIn("unparseable", ctx.exception.reason)

    def test_missing_db_raises(self):
        with self.assertRaises(BillingUnavailable) as ctx:
            query_billed_usage(UUID, db_path=Path("/no/such/local.db"))
        self.assertIn("db not found", ctx.exception.reason)

    def test_corrupt_db_raises_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.db"
            path.write_bytes(b"this is not sqlite")
            with self.assertRaises(BillingUnavailable):
                query_billed_usage(UUID, db_path=path)


class AttachTest(unittest.TestCase):
    def test_success_sets_document_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            db = make_db(base / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80})),
            ])
            document = make_document()
            attach_billed_usage(document, source, db_path=db)
            self.assertIsNotNone(document.billing)
            self.assertEqual(document.billing.prompt_tokens, 100)

    def test_failure_records_a_coverage_note_and_keeps_billing_none(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            document = make_document()
            attach_billed_usage(document, source,
                                db_path=base / "absent.db")
            self.assertIsNone(document.billing)
            self.assertEqual(
                document.coverage.notes,
                ["billed_usage: unavailable (db not found: %s)"
                 % (base / "absent.db")])

    def test_non_qoder_source_records_the_applicability_note(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tier1.jsonl"
            source.write_text("{}", encoding="utf-8")
            document = make_document()
            attach_billed_usage(document, source)
            self.assertIsNone(document.billing)
            self.assertEqual(document.coverage.notes,
                             ["billed_usage: unavailable (not a qoder session)"])
```

- [ ] **Step 2.2: run to verify RED**

```bash
PYTHONPATH=src python3 -m pytest -q tests/test_billing.py
```

Expected: collection ERROR `ModuleNotFoundError: No module named 'agent_session_detective.billing'`.

- [ ] **Step 2.3: implement (new file `src/agent_session_detective/billing.py`)**

```python
"""Billed-usage side-channel (opt-in): provider-billed token totals from
Qoder's local SharedClientCache SQLite DB.

Read-only and purely local — the DB is never written. Attach happens
post-build on explicit opt-in; the builder is untouched. Failures are
counted and reasoned (one coverage note), never guessed: a billed number
exists only when >=1 parseable token_info row was observed for the session
uuid.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ir.schema import AuditDocument, BilledUsage

DEFAULT_DB_PATH = Path(
    "~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db"
).expanduser()

SOURCE = "SharedClientCache chat_message.token_info"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class BillingUnavailable(Exception):
    reason: str


def session_uuid_from_source(source_path: Path) -> Optional[str]:
    """The SharedClientCache join key: the Qoder transcript's uuid stem."""
    source_path = Path(source_path)
    if not source_path.is_file() or source_path.suffix != ".jsonl":
        return None
    stem = source_path.stem
    return stem.lower() if _UUID_RE.match(stem) else None


def query_billed_usage(session_uuid: str, db_path=None) -> BilledUsage:
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise BillingUnavailable("db not found: %s" % path)
    rows_total = 0
    rows_malformed = 0
    prompt = completion = cached = 0
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                "SELECT token_info FROM chat_message WHERE session_id = ?",
                (session_uuid,))
            while True:
                rows = cursor.fetchmany(200)
                if not rows:
                    break
                for (raw,) in rows:
                    rows_total += 1
                    try:
                        info = json.loads(raw)
                        prompt += int(info["prompt_tokens"])
                        completion += int(info["completion_tokens"])
                        cached += int(info["cached_tokens"])
                    except (TypeError, ValueError, KeyError):
                        rows_malformed += 1
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BillingUnavailable(str(exc))
    if rows_total == 0:
        raise BillingUnavailable(
            "no billed rows for session %s" % session_uuid)
    if rows_malformed == rows_total:
        raise BillingUnavailable(
            "all %d token_info rows unparseable for session %s"
            % (rows_total, session_uuid))
    return BilledUsage(
        session_id=session_uuid,
        source=SOURCE,
        db_path=str(path),
        requests=rows_total - rows_malformed,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        rows_total=rows_total,
        rows_malformed=rows_malformed,
    )


def attach_billed_usage(document: AuditDocument, source_path, db_path=None) -> None:
    """Opt-in post-build attach: query the side-channel and set
    document.billing, or record one honest coverage note. Never raises."""
    uuid = session_uuid_from_source(Path(source_path))
    if uuid is None:
        document.coverage.notes.append(
            "billed_usage: unavailable (not a qoder session)")
        return
    try:
        document.billing = query_billed_usage(uuid, db_path=db_path)
    except BillingUnavailable as exc:
        document.coverage.notes.append(
            "billed_usage: unavailable (%s)" % exc.reason)
```

- [ ] **Step 2.4: run to verify GREEN + no regressions**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 2.5: commit**

```bash
git add src/agent_session_detective/billing.py tests/test_billing.py
git commit -m "feat: add billing.py SharedClientCache reader with attach (read-only, uuid join)"
```

---

## Task 3: CLI flags `--billed-usage` / `--billed-db`

**Files:**
- Modify: `src/agent_session_detective/cli.py` (import block, arg defs after `--tree-out`, attach call after document build)
- Modify: `tests/test_cli.py` (add `import sqlite3`, append class at end)

- [ ] **Step 3.1: write the failing tests**

In `tests/test_cli.py` add to the import block at the top (alphabetical):

```python
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
```

Append at the end of the file:

```python
BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"


class BilledUsageFlagTests(unittest.TestCase):
    def test_billed_usage_attaches_billed_totals_to_the_ir(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            db = base / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
            conn.execute("INSERT INTO chat_message VALUES (?, ?)", (
                BILLING_UUID,
                json.dumps({"prompt_tokens": 1000, "completion_tokens": 40,
                            "cached_tokens": 800})))
            conn.commit()
            conn.close()

            exit_code = cli.main([
                str(transcript), "--no-judge", "--billed-usage",
                "--billed-db", str(db),
                "--ir-out", str(base / "ir.json"),
            ])

            self.assertEqual(exit_code, 0)
            payload = json.loads((base / "ir.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["billing"]["requests"], 1)
            self.assertEqual(payload["billing"]["prompt_tokens"], 1000)
            self.assertEqual(payload["billing"]["cached_tokens"], 800)

    def test_billed_usage_unavailable_keeps_the_audit_going(self):
        from agent_session_detective import billing
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                       str(base / "absent.db")), patch(
                           "agent_session_detective.cli.render_report",
                           return_value="<html>report</html>") as render:
                exit_code = cli.main(
                    [str(transcript), "--no-judge", "--billed-usage"])

            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)
            document = render.call_args.kwargs["document"]
            self.assertIsNone(document.billing)
            self.assertTrue(any(
                note.startswith("billed_usage: unavailable")
                for note in document.coverage.notes))

    def test_without_the_flag_no_attach_happens(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "foo.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            document = render.call_args.kwargs["document"]
            self.assertIsNone(document.billing)
            self.assertEqual(document.coverage.notes, [])
```

- [ ] **Step 3.2: run to verify RED**

```bash
PYTHONPATH=src python3 -m pytest -q tests/test_cli.py -k BilledUsageFlag
```

Expected: FAIL/ERROR with `error: unrecognized arguments: --billed-usage` (argparse exits 2).

- [ ] **Step 3.3: implement**

In `src/agent_session_detective/cli.py`:

1. Import block — insert before `from .catalog import load_catalog`:

```python
from .billing import attach_billed_usage
from .catalog import load_catalog
```

2. Argument definitions — insert immediately after the `--tree-out` argument block:

```python
    parser.add_argument("--billed-usage", action="store_true",
                        help="Attach provider-billed token totals from the local "
                             "SharedClientCache DB (opt-in; Qoder transcripts only).")
    parser.add_argument("--billed-db", default=None, metavar="PATH",
                        help="Billed-usage DB path (default: Qoder SharedClientCache "
                             "local.db).")
```

3. Attach call — insert between the document build and the `--ir-out` write:

```python
    document = build_audit_document(session, _detect_adapter(session, session_source))
    if args.billed_usage:
        attach_billed_usage(document, session_source, args.billed_db)
    if args.ir_out:
```

- [ ] **Step 3.4: run to verify GREEN + no regressions**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 3.5: commit**

```bash
git add src/agent_session_detective/cli.py tests/test_cli.py
git commit -m "feat: add --billed-usage/--billed-db to the CLI (opt-in side-channel attach)"
```

---

## Task 4: tree page billed header block

**Files:**
- Modify: `src/agent_session_detective/tree_html.py` (new `_billed_header_html` helper + one line in `render_skill_tree`)
- Modify: `tests/test_tree_html.py` (appended class at end)

- [ ] **Step 4.1: write the failing tests**

Append at the end of `tests/test_tree_html.py`:

```python
class BilledHeaderTest(unittest.TestCase):
    UUID = "3b241101-e2bb-4255-8caf-4136c566a962"

    def _billed(self, **overrides):
        from agent_session_detective.ir.schema import BilledUsage
        fields = dict(
            session_id=self.UUID,
            source="SharedClientCache chat_message.token_info",
            db_path="/tmp/local.db",
            requests=120,
            prompt_tokens=15181929,
            completion_tokens=120861,
            cached_tokens=13489152,
            rows_total=120,
            rows_malformed=0,
        )
        fields.update(overrides)
        return BilledUsage(**fields)

    def test_billed_block_renders_totals_derived_numbers_and_provenance(self):
        doc = document(agents=[agent(MAIN)])
        doc.billing = self._billed()
        out = render_skill_tree(doc)
        self.assertIn("billed (provider): 120 requests", out)
        self.assertIn("prompt 15181929", out)
        self.assertIn("completion 120861", out)
        self.assertIn("cached 13489152", out)
        self.assertIn("non-cached 1692777", out)
        self.assertIn("cache-hit 88.9%", out)
        self.assertIn("SharedClientCache chat_message.token_info", out)

    def test_document_without_billing_renders_no_billed_block(self):
        out = page(agents=[agent(MAIN)])
        self.assertNotIn("billed (provider)", out)

    def test_unavailable_note_renders_as_a_notice(self):
        doc = document(agents=[agent(MAIN)])
        doc.coverage.notes.append(
            "billed_usage: unavailable (no billed rows for session %s)"
            % self.UUID)
        out = render_skill_tree(doc)
        self.assertIn("class='notice'", out)
        self.assertIn("billed_usage: unavailable", out)
```

- [ ] **Step 4.2: run to verify RED**

```bash
PYTHONPATH=src python3 -m pytest -q tests/test_tree_html.py -k BilledHeader
```

Expected: FAIL — `"billed (provider): 120 requests" not found`; the unavailable-note test fails on the missing notice.

- [ ] **Step 4.3: implement**

In `src/agent_session_detective/tree_html.py`, add before `render_skill_tree`:

```python
def _billed_header_html(document: AuditDocument) -> str:
    billed = document.billing
    if billed is not None:
        non_cached = billed.prompt_tokens - billed.cached_tokens
        hit = ("%.1f%%" % (billed.cached_tokens * 100.0 / billed.prompt_tokens)
               if billed.prompt_tokens else "?")
        return ("<div class='meta'>billed (provider): %d requests / prompt %d / "
                "completion %d / cached %d / non-cached %d / cache-hit %s · "
                "rows %d total, %d malformed · source %s · db %s</div>" % (
                    billed.requests, billed.prompt_tokens,
                    billed.completion_tokens, billed.cached_tokens, non_cached,
                    hit, billed.rows_total, billed.rows_malformed,
                    esc(billed.source), esc(billed.db_path)))
    for note in document.coverage.notes:
        if note.startswith("billed_usage: unavailable"):
            return "<div class='notice'>%s</div>" % esc(note)
    return ""
```

In `render_skill_tree`, replace the closing section of the header extend:

```python
        "<div class='meta'>costs are EST (estimated from observed body "
        "tokens); unavailable means no body was observed — a stub-derived "
        "number is never shown.</div>",
        "</header>",
        "<main>",
    ])
```

with:

```python
        "<div class='meta'>costs are EST (estimated from observed body "
        "tokens); unavailable means no body was observed — a stub-derived "
        "number is never shown.</div>",
    ])
    billed_html = _billed_header_html(document)
    if billed_html:
        parts.append(billed_html)
    parts.extend([
        "</header>",
        "<main>",
    ])
```

- [ ] **Step 4.4: run to verify GREEN + no regressions**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 4.5: commit**

```bash
git add src/agent_session_detective/tree_html.py tests/test_tree_html.py
git commit -m "feat: render the billed-usage block in the tree page header"
```

---

## Task 5: web route `GET /api/tree?billed=1`

**Files:**
- Modify: `src/agent_session_detective/web.py` (`render_tree_page` signature + `/api/tree` route param)
- Modify: `tests/test_web.py` (add `import sqlite3`, append three tests to `TreeRouteTests`)

- [ ] **Step 5.1: write the failing tests**

In `tests/test_web.py` add `import sqlite3` to the top import block (alphabetical, after `import shutil`).

Append inside the `TreeRouteTests` class (after `test_degraded_cli_session_renders_with_a_notice`):

```python
    def test_billed_param_renders_billed_totals_from_the_side_channel(self):
        from agent_session_detective import billing
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "3b241101-e2bb-4255-8caf-4136c566a962.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            db = base / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
            conn.execute("INSERT INTO chat_message VALUES (?, ?)", (
                "3b241101-e2bb-4255-8caf-4136c566a962",
                json.dumps({"prompt_tokens": 1000, "completion_tokens": 40,
                            "cached_tokens": 800})))
            conn.commit()
            conn.close()
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = db
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)

            with urllib.request.urlopen(
                    self.tree_url(str(transcript)) + "&billed=1") as response:
                page = response.read().decode("utf-8")

            self.assertIn("billed (provider): 1 requests", page)
            self.assertIn("prompt 1000", page)

    def test_billed_param_with_unavailable_side_channel_renders_the_note(self):
        from agent_session_detective import billing
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "3b241101-e2bb-4255-8caf-4136c566a962.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            original = billing.DEFAULT_DB_PATH
            billing.DEFAULT_DB_PATH = base / "absent.db"
            self.addCleanup(setattr, billing, "DEFAULT_DB_PATH", original)

            with urllib.request.urlopen(
                    self.tree_url(str(transcript)) + "&billed=1") as response:
                page = response.read().decode("utf-8")

            self.assertIn("billed_usage: unavailable", page)

    def test_no_billed_param_renders_no_billed_block(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with urllib.request.urlopen(self.tree_url(str(transcript))) as response:
                page = response.read().decode("utf-8")

            self.assertNotIn("billed (provider)", page)
```

- [ ] **Step 5.2: run to verify RED**

```bash
PYTHONPATH=src python3 -m pytest -q tests/test_web.py -k TreeRoute
```

Expected: FAIL — billed=1 renders a page without the billed block (the param is ignored).

- [ ] **Step 5.3: implement**

In `src/agent_session_detective/web.py`, change `render_tree_page`:

```python
def render_tree_page(session_path: str, billed: bool = False) -> str:
    """The actual skill tree for one session, as a standalone HTML page.

    Same IR projection the audit job uses, minus judge and catalog: the
    tree derives from the document alone, so no audit cache and no
    background job are involved. ``billed=True`` opt-in attaches the
    provider-billed side-channel before rendering (no db-path over HTTP).
    """
    from .ir.builder import build_audit_document
    from .tree_html import render_skill_tree

    source_path = Path(session_path).expanduser()
    session = load_session(source_path)
    if not session.events:
        raise ValueError("no events parsed")
    document = build_audit_document(session, _ir_adapter_id(session, source_path))
    if billed:
        from .billing import attach_billed_usage
        attach_billed_usage(document, source_path)
    return render_skill_tree(document)
```

In `Handler.do_GET`, change the `/api/tree` branch:

```python
        elif route == "/api/tree":
            params = parse_qs(parsed.query)
            path = (params.get("path") or [""])[0]
            if not path:
                self._send_json(400, {"error": "path required"})
                return
            if not Path(path).expanduser().exists():
                self._send_json(404, {"error": "session not found"})
                return
            try:
                page = render_tree_page(
                    path, billed=(params.get("billed") == ["1"]))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_html(page)
```

- [ ] **Step 5.4: run to verify GREEN + no regressions**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass.

- [ ] **Step 5.5: commit**

```bash
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: serve billed usage on GET /api/tree?billed=1"
```

---

## Task 6: full suite + real-session smoke

- [ ] **Step 6.1: full suite**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: all pass, no warnings introduced.

- [ ] **Step 6.2: smoke on the newest real Qoder transcript (outputs to /tmp only)**

```bash
TRANSCRIPT=$(ls -t ~/.qoder/projects/*/transcript/*.jsonl 2>/dev/null | head -1)
PYTHONPATH=src python3 -m agent_session_detective.cli "$TRANSCRIPT" --no-judge \
  --billed-usage --tree-out /tmp/asd-billed-smoke.html --ir-out /tmp/asd-billed-smoke.json
grep -o "billed (provider)[^<]*" /tmp/asd-billed-smoke.html | head -1
```

Expected: either a billed block (DB has rows for that session) or the honest `billed_usage: unavailable (...)` notice — never a fabricated number. `python3 -c "import json;d=json.load(open('/tmp/asd-billed-smoke.json'));print(d['billing'] is not None or any(n.startswith('billed_usage:') for n in d['coverage']['notes']))"` prints `True`. Nothing from the session is committed; /tmp artifacts stay out of the repo.

- [ ] **Step 6.3: final report**

Summarize: gate result, pinned IR_VERSION value, commit hashes per task, test count, smoke outcome.
