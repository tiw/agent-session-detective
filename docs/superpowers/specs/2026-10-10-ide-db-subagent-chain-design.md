# IDE DB Subagent Chain Design

## Goal

ASD's dispatch join sees only subagent transcripts that exist as files on
disk. IDE-channel sub-agent work in the same session is invisible: session
b1d65022 (`b1d65022-d35d-4a45-b24f-27eb970e6b86`, title 外卖订单消息发 MetaQ 方案)
renders **9 orphan dispatches** — every parent `Agent` call has a real child
conversation in Qoder's local SharedClientCache DB
(`chat_session.parent_tool_call_id`, measured 9/9 matching transcript tool
call ids like `call_983731501954445b96a68587`), yet no subagent file exists
in the transcript directory. The tree shows "dispatch target has no matching
subagent transcript" nine times; the sub-agents' briefs, requests, skill
loads and executions are all missing from the audit.

This change reads that chain (direct children of the root session),
decrypts `chat_message.content` (AES-128-CBC, key = IV =
`QbgzpWzN7tfe43gf`), synthesizes wire events per child session, and lets
them ride the **existing records-path join** (`ref.parent_tool_use_id`).
After it, those dispatches join, briefs re-classify (D3), and per-child
requests / skill loads / executions enter the audit and the skill tree.

Opt-in side-channel (D2); the pure transcript path stays the default fact
source. Measured corpus for acceptance: 9 children, 415 message rows
(39/23/16/102/28/85/46/31/45 per child), ~11 MB content ciphertext,
`token_info` present on 164/164 assistant rows, 0 grandchildren.

## Decisions (locked with user)

1. **D1 — direct children only.** `parent_session_id = <root uuid>`, one
   level. Grandchildren were 0 in the measured corpus; recursion is a
   non-goal.
2. **D2 — opt-in, behaviorally inert when off.** Flag off ⇒ no DB open, no
   synthesized events, no coverage notes; dispatch/coverage counts
   identical. The only schema-visible deltas are the IR version bump and
   the new empty `ide_db` coverage block (the established additive
   contract).
3. **D3 — decryption ladder, lazy.** `cryptography` (C-backed) → `openssl
   enc -d -aes-128-cbc` subprocess → honest unavailability. The ladder
   resolves once per attach; no backend ⇒ attach records unavailable +
   coverage note, synthesizes nothing, never raises. Per-row failure with
   a working backend ⇒ that row's payload is skipped but its plaintext
   `token_info` UsageRecord is still emitted, counted in
   `decrypt_failures`. Zero-dep posture preserved (no pyproject change).
4. **Join by the existing rule.** Synthesized children carry
   `ref.parent_tool_use_id`; the string-equality records-path join in
   `dispatch.py` is untouched except for provenance counting. Id formats
   are never parsed (standing invariant).
5. **Attach point (approach A).** `attach_ide_db(session, source, db)`
   runs after `load_session` and before `build_audit_document`, mutating
   the `Session` only. Rejected: post-build injection (duplicates
   classification → drift) and orphan-annotation-only (fails the goal).
6. **Disk precedence.** A DB child whose `parent_tool_call_id` is already
   claimed by disk records or meta.json `toolUseId` is skipped
   (`skipped_already_joined`); no duplicate agents, no double counting.
7. **CLI flag pair mirrors billing.** `--ide-db` (store_true) +
   `--ide-db-path` (PATH, default `None` → `ide_db.DEFAULT_DB_PATH`).
   Satisfies the approved `--ide-db [PATH]` intent.

## Layering

`ide_db.py` is the second side-channel beside `billing.py`, and the only
one whose product is wire-layer input rather than document decoration:

```
billing.py                  ide_db.py                     builder (ir/)
──────────                  ─────────                     ──────────────
DEFAULT_DB_PATH  ──import──►  attach_ide_db(session, source, db)
session_uuid_from_source ──►   query children (read-only)
                               decrypt (lazy ladder)
                               synthesize Events ──────► session.events
                               set session.ide_db_stats ─► CoverageReport.ide_db
                                                           + coverage note
```

`ide_db` imports from `wire` (Event, Session) and reuses billing's
`DEFAULT_DB_PATH` + `session_uuid_from_source` (same DB); `wire`/`ir/*`
never import `ide_db`. The attach is a caller-side step (cli / web),
exactly like `attach_billed_usage`.

## Data model

- `wire.Session` gains `ide_db_stats: Optional[dict] = None` (mirrors the
  `subagent_meta` pattern). Set only by `attach_ide_db`.
  - attached: `{"available": True, "db_path": str, "children_found": int,
    "synthesized": int, "rows": int, "decrypt_failures": int,
    "skipped_already_joined": int}`
  - unavailable: `{"available": False, "reason": str, "db_path": str}`
  - countable semantics: `children_found` = DB rows matching the child
    query; `skipped_already_joined` = of those, ids already claimed by
    disk; `synthesized = children_found - skipped_already_joined` (each
    contributes its message rows' events); `rows` = message rows read
    across synthesized children; `decrypt_failures` = rows whose payload
    lacked a usable decrypted JSON (their plaintext-usage `UsageRecord`
    still counts toward `rows`).
- `schema.CoverageReport` gains `ide_db: dict = field(default_factory=dict)`
  (mutable-default rule; same declaration as `dispatch_links`) — the stats
  dict verbatim (same "lands in coverage verbatim" contract); `{}` when
  the flag was off.
- `dispatch_links` gains `joined_via_ide_db: int`. Extended invariant:
  `joined + joined_via_meta_only + joined_via_ide_db + orphan_dispatches
  == len(dispatches)`.
- `IR_VERSION` 1.6 → **1.7** (additive).

## Row → event mapping

Per child: `source = session.directory / ("ide-db-%s.jsonl" % kid_uuid)`
(synthetic path; `Event.source` is a `Path`), `origin =
"subagent:ide-db:<kid-uuid>"`, `seq` = DB row order (`ORDER BY id`),
`ts = gmt_create / 1000` (ms epoch, `None` when absent). Every event of
the child carries the same
`ref = {"parent_tool_use_id": <child's parent_tool_call_id>,
"request_id": <row's request_id>}` — so `attribute_agents` groups it as
`qoder:subagent-file` and the join's first-record scan finds the link.

| row | synthesized events (in order) | source of text |
|---|---|---|
| `role=user` | `TurnBegin{user_input:[{type:text, text:...}]}` | decrypted `contents` text parts; skipped when empty |
| `role=assistant` | `ContentPart{type:text}` if `content` non-empty; `ContentPart{type:think}` if `reasoning_content` non-empty; `ToolCall{id, function:{name, arguments}}` per `tool_calls[i]` (arguments kept as the JSON string); `UsageRecord` | decrypted JSON; usage from plaintext `token_info` |
| `role=tool` | `ToolResult{tool_call_id, return_value:{output: content, is_error: <decrypted is_error or false>}}` | decrypted JSON |

`UsageRecord` payload: `input_other = max(0, prompt_tokens - cached_tokens)`
(prompt includes cached — measured), `input_cache_read = cached_tokens`,
`output = completion_tokens`. No `model`, no cache-creation counters, no
`context_usage_ratio` (not in `token_info`). No `RuntimeConfig`/`ActiveLeaf`
synthesis: model / context_window stay `None` → tree badge shows unknown.
An assistant row with no parseable usage telemetry emits no `UsageRecord`
and therefore does not close a span — it merges forward to the next closed
span (recorded fact, never guessed).

Consequences inherited from existing rules (no new code): one assistant
DB row = one `WireRecord` (same `(source, seq)` merges) with positive usage
= exactly one `RequestSpan` = one `LLMCall`, so tree `n_requests` ==
assistant rows per child; children are identity tier 2 (`request_id`, no
`request_hash`) → no anchor verification, no tier-3 note; the first
user-item becomes the dispatch brief (D3 re-bucket to `inject`, or tier-A
`skill_body` if signed); main-only gates keep child user rows out of
intervention classification and `human_text`.

## Query and disk precedence

Root uuid via billing's `session_uuid_from_source` (None → unavailable
"not a qoder session"). Read-only connect, same as billing:
`sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)`.

```sql
SELECT session_id, parent_tool_call_id FROM chat_session
WHERE parent_session_id = ? AND parent_tool_call_id != ''
  AND session_type LIKE 'agent_sub%'
ORDER BY session_id
```

Per child: `SELECT id, role, content, request_id, token_info, gmt_create
FROM chat_message WHERE session_id = ? ORDER BY id`.

`skipped_already_joined` = children whose `parent_tool_call_id` ∈ claimed
set = {disk records' `ref.parent_tool_use_id`} ∪ {subagent_meta
`toolUseId`}. A synthesized child matching **no** parent tool call is not
dropped: the existing join counts it in `unmatched_subagent_files` and the
tree lists it under `loose`.

## dispatch.py change

Counting branch only (no join change). For a joined row whose
`subagent_agent_id` starts with `subagent:ide-db:`, increment the new
`joined_via_ide_db` instead of `joined`:

```python
if row["link_source"] == "records":
    if subagent_agent_id.startswith("subagent:ide-db:"):
        joined_via_ide_db += 1
    else:
        joined += 1
else:
    joined_via_meta_only += 1
```

`dispatch_links` gains `"joined_via_ide_db"`. All other subagent
mechanics (brief, D3, phase refs) apply automatically because
`_is_subagent` is prefix-based.

## Coverage note and builder wiring

Builder passes `session.ide_db_stats` into `build_coverage` (new
`ide_db` param → `CoverageReport.ide_db`) and extends `_notes(...)`:

- attached → `"ide_db: attached (children=%d synthesized=%d rows=%d
  decrypt_failures=%d skipped_already_joined=%d)"`
- unavailable → `"ide_db: unavailable (%s)"`

With the flag off, `ide_db_stats` is `None` → no note, empty block.

## Caller wiring

- **CLI** (`cli.py`): after `load_session` (116) and before
  `build_audit_document` (127):
  `if args.ide_db: attach_ide_db(session, session_source, args.ide_db_path)`.
  New flags beside the billing pair (63-68).
- **Web** (`web.py`): `/api/tree` (749) parses `ide_db` mirroring
  `billed=(params.get("billed") == ["1"])` (760) →
  `render_tree_page(session_path, billed=..., ide_db=...)`, which calls
  `attach_ide_db` between its `load_session` and `build_audit_document`.
  The tree page never went through the audit cache/judge fingerprint —
  **no fingerprint bump, no `cache_key` change**; tests monkeypatch
  `DEFAULT_DB_PATH` (no db-path over HTTP). v1 = CLI + `/api/tree` only.

## Surfaces

- **Tree footer** (`tree_html.py` stat line, 98-102): add
  `" / joined via ide-db %d"`, rendered only when `joined_via_ide_db > 0`
  (flag-off pages stay byte-identical).
- **Tree edges**: orphan edges become joined edges; child nodes get the
  existing `_label` treatment (`subagent_type · <8 hex>` from the id's
  trailing hex run); `n_requests`, brief cost, attachment rows (loads /
  executions) fill in.
- `document.source_files` lists the synthetic `ide-db-<uuid>.jsonl` paths
  (provenance: which children were read from the DB — intended).

## Non-goals

- No recursion into grandchildren (D1); no experts-mode
  `agents/*.output` ingestion (empty on this machine, separate source).
- No `chat_message.tool_result` envelope parsing; the decrypted `content`
  blob is the only text source.
- No session-page (audit UI) integration, no fleet access, no judge-cache
  or fingerprint change.
- No hard `cryptography` dependency, no schema change to `Dispatch`,
  no new join mechanism.

## Testing (TDD)

- `tests/test_ide_db.py` (new): tmp sqlite fixture (chat_session +
  chat_message) with monkeypatched decryptor for mapping tests —
  user/assistant/tool row → event shapes; usage arithmetic
  (`input_other = prompt − cached`, clamped); undecryptable row →
  UsageRecord still emitted + `decrypt_failures` counted; claimed ids →
  `skipped_already_joined`; query filter (`session_type`, non-empty
  parent id); ladder absent → unavailable stats + note, zero synthesis.
- Decryption: one precomputed ciphertext constant (generated during
  implementation), gated `pytest.importorskip("cryptography")`; the
  openssl rung is exercised only when `cryptography` is absent (skipped
  otherwise).
- Join/invariant: fixture session → `build_audit_document` →
  `joined_via_ide_db == N`, `orphan_dispatches == 0`,
  `joined + joined_via_meta_only + joined_via_ide_db + orphan ==
  len(dispatches)`, tree edges have non-null `to_agent_id`, brief
  re-bucketed to `inject`.
- `tests/test_cli.py` (append): `--ide-db` on (monkeypatched default DB)
  → footer shows "joined via ide-db"; off → no segment, no notes, counts
  unchanged.
- Goldens / version pins updated 1.6 → 1.7.

## Known consequences

- IR 1.6 → 1.7: new `CoverageReport.ide_db` block, new
  `dispatch_links.joined_via_ide_db` key, new `Session` field.
- `docs/qoder-transcript-shape.md` correction folded into this change:
  the doc claims `load_session` rejects qoder-cli with `ValueError`;
  `wire.py` (691-701) actually loads it degraded
  (`source_format="qoder-cli"`, `compaction_telemetry_available=False`).
- Kids show no model / context window (no `RuntimeConfig` synthesized)
  and tier-2 identity (no anchor cross-check) — both are honest gaps,
  visible as such.
- b1d65022 acceptance: 9 orphans → 0, `joined_via_ide_db = 9`, sub-agent
  briefs/requests/loads appear in tree nodes; ciphertext never leaves the
  process (local-only side-channel).

## Concurrency discipline (shared worktree)

1. `git status --porcelain` shows no tracked-file modifications before
   starting; pre-existing red baseline → stop and wait for the user.
2. Stage only task-owned files by explicit path; never `git add -A`; no
   push without explicit user request.
3. Foreign untracked files are never staged.
