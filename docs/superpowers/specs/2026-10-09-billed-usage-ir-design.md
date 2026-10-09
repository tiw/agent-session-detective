# Billed Usage Side-Channel — IR First-Class (Approach A) Design

## Goal

Let ASD show **provider-billed token totals** (what the bill actually says)
for a session, next to its estimated per-skill costs — without polluting the
transcript-only measurement basis. The data comes from Qoder's local
SharedClientCache SQLite DB (`chat_message.token_info`, one row per LLM
request, keyed by `session_id` = transcript uuid — verified against session
b1d65022: 120 requests / 15,181,929 prompt / 13,489,152 cached).

Two measurement lenses, never mixed:

- **EST** (existing): estimated per-item/per-skill costs derived from
  transcript content. Unchanged everywhere.
- **Billed** (new): provider-reported per-request totals from the cache DB.
  Session-level only — the DB has no per-skill attribution. Opt-in.

## Decisions (locked with user)

1. **Enablement: explicit opt-in flag.** Default OFF; zero DB contact when
   off; audit stays pure-transcript and reproducible. Provenance is declared
   on the page whenever billed numbers appear.
2. **Architecture: IR first-class (Approach A).** `BilledUsage` becomes a
   dataclass in `ir/schema.py`; `AuditDocument.billing` carries it; IR JSON
   exports include it. The concurrent session's IR 1.3 must be committed
   first — implementation is gated on that (see Concurrency).

## Architecture

Builder stays untouched — billed is a **post-build, explicit attach step**:

```
load_session ──► build_audit_document ──► document
                                            │
                    --billed-usage / &billed=1
                                            ▼
                     billing.attach_billed_usage(document, source_path)
                       ├─ success          → document.billing = BilledUsage
                       └─ BillingUnavailable → coverage.notes line, billing stays None
                                            ▼
                     ir-out JSON / render_skill_tree read it
```

Components:

1. **`ir/schema.py`** — pure data:
   - `BilledUsage` dataclass (fields below).
   - `AuditDocument.billing: Optional[BilledUsage] = None`, inserted after
     `phases`, before `ir_version` (asdict key order follows field order).
   - `IR_VERSION` bump: new value = HEAD's current `IR_VERSION` + one minor
     ("1.3" → "1.4" if the concurrent IR 1.3 landed; "1.2" → "1.3" if it
     never landed and was reverted). Pin the concrete value at execution time.

2. **`billing.py`** (new module, stdlib `sqlite3` only) — the I/O layer:
   - `DEFAULT_DB_PATH = ~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db`
   - `session_uuid_from_source(source_path) -> Optional[str]` — Qoder only:
     the transcript file's stem must be a uuid shape (8-4-4-4-12 hex);
     anything else (Kimi dirs, Codex rollouts) → `None`.
   - `class BillingUnavailable(Exception)` with `.reason`.
   - `query_billed_usage(session_uuid, db_path=DEFAULT_DB_PATH) -> BilledUsage`
     — read-only URI connect (`file:...?mode=ro`); scan
     `SELECT token_info FROM chat_message WHERE session_id=?` row by row and
     parse each `token_info` JSON in Python (row counts are hundreds at most).
   - `attach_billed_usage(document, source_path, db_path=None) -> None` —
     the single entry point used by CLI and web; on failure appends exactly
     one `coverage.notes` line `billed_usage: unavailable (<reason>)` and
     leaves `document.billing = None`.

## Data shape

```python
@dataclass
class BilledUsage:
    session_id: str        # transcript uuid used for the join
    source: str            # constant: "SharedClientCache chat_message.token_info"
    db_path: str           # db file actually queried (provenance)
    requests: int          # good rows = billed LLM requests
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    rows_total: int        # all chat_message rows seen for the session
    rows_malformed: int    # skipped rows (missing/unparseable token_info)
```

Derived values (render-time only, never stored): non-cached input =
`prompt_tokens - cached_tokens`; cache-hit ratio = `cached / prompt`.

## Failure and degrade semantics

Every failure is **counted + reasoned, never guessed**; the EST page renders
unchanged:

| case | outcome |
|---|---|
| db file missing | `BillingUnavailable("db not found: <path>")` |
| sqlite error (locked / corrupt / table missing) | `BillingUnavailable(<sqlite message>)` |
| zero `chat_message` rows for uuid | `BillingUnavailable("no billed rows for session <uuid>")` |
| malformed `token_info` rows | skipped, counted in `rows_malformed`; success iff ≥ 1 good row |
| source not a Qoder session | note `billed_usage: unavailable (not a qoder session)` |

When the flag is on but the query failed, the tree page shows the
unavailable reason; when the flag is off, nothing changes anywhere.

## Surfaces

- **CLI**: `--billed-usage` (store_true), `--billed-db PATH` (default
  `DEFAULT_DB_PATH`). Attached right after `build_audit_document`, before
  `--ir-out` / `--tree-out` rendering, so both carry billed data.
- **Web**: tree route accepts `billed=1` → `render_tree_page` attaches with
  `DEFAULT_DB_PATH` (no db-path parameter over HTTP; tests monkeypatch the
  module constant).
- **Tree page** (`tree_html.py`): when `document.billing` exists, the header
  gains a session-level **billed usage** block: requests, prompt /
  completion / cached / derived non-cached input, cache-hit %, row counts,
  and a provenance line (source + db path). When only the coverage note
  exists, a single unavailable line with the reason. Skill-level costs
  remain EST and are never recomputed or relabeled.

## Non-goals

- Per-skill billed attribution — the DB has no per-skill data; do not
  fabricate.
- Billed numbers in the main report page — `token_stats` (wire telemetry)
  already covers it there.
- Any change to EST numbers, buckets, or the degrade-banner logic.
- Writing to the DB, or reading any session's rows other than the audited one.

## Security constraints

- Read-only SQLite URI; the DB is never written.
- Purely local: no network, no telemetry; page HTML stays self-contained
  (no JS, no external URLs).
- Fixtures use synthetic uuids and token counts; no real session content in
  the repo.

## Testing (TDD)

- `tests/test_ir_schema.py` (append): `billing` defaults to `None`;
  `to_dict()` carries the key; `IR_VERSION` equals the pinned new value.
- `tests/test_billing.py` (new): tmp sqlite fixtures — uuid derivation
  (ok / not-a-uuid); happy-path totals; malformed rows counted; zero rows →
  unavailable; missing db → unavailable; missing table → unavailable;
  `attach_billed_usage` success (sets `document.billing`) and each failure
  (notes line, billing stays `None`).
- `tests/test_cli.py` (append): `--billed-usage` with a tmp db → `--ir-out`
  JSON contains the block; without the flag → `"billing": null`.
- `tests/test_tree_html.py` (append): render with / without `document.billing`.
- `tests/test_web.py` (append): tree route `billed=1` renders the block
  (monkeypatched db); default route unchanged.

## Concurrency discipline (shared worktree)

Implementation runs only after the gate passes:

1. `git status --porcelain` shows **no tracked-file modifications** (the
   concurrent IR 1.3 session committed or withdrew; untracked foreign files
   may remain).
2. Baseline suite green (`PYTHONPATH=src python3 -m pytest -q`) before any
   change; pre-existing red → stop and wait for the user.
3. Foreign untracked files (`docs/qoder-transcript-shape.md`,
   `ir/intervention.py`, `tests/test_ir_intervention.py`) are never staged.
4. Stage only task-owned files by explicit path; never `git add -A`; never
   push without explicit user request.
