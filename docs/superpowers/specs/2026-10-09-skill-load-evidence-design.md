# Skill Load Evidence and Dispatch Detection Design

## Goal

Close the two evidence boundaries that currently make a coding-v2 audit
untrustworthy, without instrumentation and without fabrication:

1. **Load cost evidence.** The user-facing surfaces present a stub-derived
   number (`~7-14 tokens`, computed from the `Launching skill: X` marker
   text) as if it were the skill's load cost. The skill bodies do land in
   the transcript, but no current surface joins stub to body. Deliver an
   evidence-backed per-load ledger: every load either carries a cost
   derived from its actual body item (EST, labeled) or is explicitly
   marked `unavailable` — never a stub-derived number.
2. **Dispatch-inlined loading.** coding-v2-style workflows load their phase
   files by inlining them into subagent dispatch prompts. ASD today
   recognizes only `Skill` tool calls and SKILL.md `Read`s, so every such
   load is invisible. Deliver dispatch detection: join subagent transcripts
   to their parent `Agent` tool calls, classify the dispatch brief, and
   recognize phase/skill file references under a versioned, calibration-fed
   rule set.

Both feed the third step — **landing path ③**, a baseline run over a real
coding-v2 session — which stays gated on the user supplying that session.

The founding disciplines carry over unchanged: coverage is re-derived,
never self-declared; telemetry the source does not record is labeled
unavailable, never fabricated.

## Scope

In scope (IR 1.1):

- `ir/loads.py`: the load-evidence ledger, reading only the
  `AuditDocument`.
- `ir/dispatch.py`: dispatch join, brief classification, orphan
  accounting, phase reference extraction.
- Parser deltas in `wire.py` / `items.py` that the join requires —
  `parent_tool_use_id`, `.meta.json` siblings, tool call/result ids.
- `ir/phase_rules.py`: a versioned rule set that **ships empty** until
  calibration ③ derives real patterns.
- Schema additions: `LoadEvidence`, `Dispatch`, `PhaseEntity`, two new
  `ContentItem` fields, three `CoverageReport` blocks; `IR_VERSION`
  1.0 → 1.1.
- Surfaces: `report.py` (new evidence section + marker-only fix),
  `webapp` (same fix in `app.js`, `skill_loads` in the result,
  fingerprint bump), `cli.py` (always build the document).
- Tests per the Tests section, including red-green regressions for both
  render fixes.

Out of scope (1.1):

- **No instrumentation.** The user-facing app must never self-report its
  own context share.
- **No hardcoded phase patterns.** Until ③ calibrates a rule set against
  a real coding-v2 session, pattern rules do not exist and recognition
  degrades to tier C (counted, `phase_id: null`).
- No judgment on whether phase-inlined loading is good or bad —
  measurement only.
- No web UI redesign; the new surfaces render through the existing
  report layer, and the web app remains local-only (127.0.0.1, no auth,
  never exposed).
- Kimi/Codex dispatch detection: the contract shape is defined here, but
  implementations land after their subagent formats are verified the same
  way Qoder's was (their document fields stay empty lists, not guesses).
- No baseline runs on stale sessions (the 2026-07-09 corpus is retired —
  see Landing path ③).

## Motivation: two verified evidence boundaries

### Boundary 1 — the load-cost distortion is user-facing, not a parse gap

The original claim "SKILL.md bodies never land in the log" is wrong and is
retired. In Qoder harness v1.1.64, bodies land through four carriers:

- per-load `invoked_skills` attachment deltas;
- post-compaction `invoked_skills` re-injection (observed: 5 records
  ≥ seq 1076, ~105KB cumulative in one session);
- plain string-content user records immediately after the stub
  (observed: stub@25 → body@27, stub@458 → body@460; another session
  stub@18 → body@24 with `ARGUMENTS:` appended);
- `hook_output` attachments can carry a full body (observed: a
  SessionStart hook body at seq 6).

The IR already classifies all of these correctly (`ir/skills.py`
`BODY_CHANNELS` plus the `tool:read` channel for Read-loaded bodies,
`SkillEntity` observations with `body_sha1`). What is broken is the
**legacy user-facing path**: it renders `timeline.loads[].tokens_est`,
which is derived from the ~42-59 char stub text. Two surfaces render that
number, and a third carries the field:

- `report.py:391-397` — summary meta `~%d tokens`;
- `webapp/app.js:612` — `· ~" + l.tokens_est + " tokens`;
- `web.py:138` carries the same field in the raw timeline JSON (no
  render there; the field stays because the legacy path keeps driving
  counts and the judge).

Nothing joins stub to body in that path. The fix is therefore a ledger
plus a render rule — not a parser fix:

> **Render rule (unconditional): no stub-derived number is ever rendered
> as a load cost. Load costs render only from the evidence ledger, labeled
> EST; a load without a body renders `unavailable`.**

### Boundary 2 — dispatch-inlined loads are outside the detection scope

In the observed v1.1.64 corpus, main-side subagent dispatch is a tool
call named `Agent` with input keys `description` / `prompt` /
`subagent_type`. Today it is classified as a generic `assistant/tool_call`
with no dispatch identity, and the subagent transcript's first record (a
string-content user record carrying the dispatch brief) is classified as a
generic `user/user_message`. In one observed corpus (main transcript + 66
subagent files, Qoder 1.1.64): 66 dispatches, briefs 2,013-46,320 chars.

The linkage is already in the files, verified 66/66 in that corpus by two
independent paths:

- **every record** of a subagent transcript carries `parent_tool_use_id`
  (e.g. `call_00_kaGJzguq4k0zd0WEoy6O0999`) equal to the main-side
  `Agent` tool_use id **by string equality**; and
- the sibling `agent-*.meta.json` carries the same id as
  `{"agentType": "general-purpose", "toolUseId": "call_00_…",
  "description": "…", "invocationName": "general-purpose"}`.

Design rule that follows: **never parse id formats — join by string
equality only**, with meta.json as a counted fallback when records lack
the field. No sidechain records exist in that corpus's main transcript;
dispatch lives exclusively in subagent files in this version, but the
join does not depend on that.

Whether a dispatch brief is a skill body is corpus-relative and must not
be pre-judged: in that corpus, 0/66 briefs start with a skill signature
and 4/66 mention signature-shaped text somewhere; both figures are
corpus-relative and are recorded as coverage observations, never as
constants.

## Landing path ①: evidence-backed load ledger

### LoadEvidence

New dataclass in `ir/schema.py`, produced by `ir/loads.py`:

```
LoadEvidence:
  load_id: str          # "<agent_id>:load:<n>", wire order, n per agent
  agent_id: str
  skill_id: str
  kind: str             # "load" | "reload"
  marker_item_id: str | null    # the skill_stub item, when one exists
  body_item_id: str | null
  body_sha1: str | null
  channel: str | null   # body channel; null when unavailable
  ts: float | null
  cost_tokens_est: int | null   # null <=> cost_basis "unavailable"
  cost_basis: str       # "body" | "unavailable"
```

- `cost_tokens_est` is the attached body item's EST `tokens_est` — the
  body the log actually carries. It is never derived from the stub.
- `channel` exposes the body composition (`invoked_skills`,
  `hook_output`, `user_signature`, `tool:read`), so a later calibration
  can argue for a stricter SKILL.md-only rule if the data supports it.
- Loads are stored on the document (`AuditDocument.skill_loads`) in wire
  order, consistent with `SkillEntity`.

### Join rule

A deterministic single walk per `(agent_id, skill_id)` over that agent's
items in wire order, using only `SkillEntity` observations and item
liveness (`gone_seq`) — no re-parsing:

1. A `skill/skill_stub` item opens a pending load.
2. The **next body** of the same skill in that agent, before the next
   stub of that skill, attaches to the open stub and closes it — first
   body wins per stub; later bodies are not attached to it.
3. A body with no open pending load becomes a load row of its own:
   - first body ever for the skill in that agent → `kind: "load"`,
     `marker_item_id: null`;
   - otherwise compare against the last attached body of that skill in
     that agent: previous body's `gone_seq is not None` (compacted away)
     → `kind: "reload"` (a real re-injection — its cost is counted);
     previous body still alive → **not** a load row; counted in
     `redundant_bodies`, with no cost attached (the duplication cost
     belongs to the redundancy analysis via `duplicate_groups` — counting
     it here too would double-count).
4. A pending stub still unattached when its successor stub arrives, or at
   end of walk → load row with `body_item_id: null`,
   `cost_tokens_est: null`, `cost_basis: "unavailable"`, `channel: null`.
   The marker is kept (`marker_item_id`) so the gap stays inspectable.

Read-loaded bodies (channel `tool:read`) participate in the ledger — a
Read of a SKILL.md is a load-with-cost like any other, and the channel
column is what tells the two apart.

### Analyses

`analyses.py` gains `skill_loads(document)`: the ledger rows plus rollups
— totals for loads / reloads / redundant_bodies / unavailable; summed
cost over non-null rows (EST, labeled); channel distribution.

### Surfaces

- `report.py`: new section **"Skill loads (evidence-backed)"** — per load:
  skill, agent, time, channel, cost or `unavailable`; totals including
  reloads and redundant bodies. The legacy "Skill Lifecycle" section's
  summary meta drops the `~%d tokens` segment entirely (replaced by a
  marker-only label, e.g. `load marker`) — unconditionally, so non-Qoder
  adapters get the honest label even when no evidence table exists.
  Legacy `timeline.loads` keeps driving counts, expectations, and the
  judge — no behavior change there.
- `webapp/app.js`: same rule at line 612; the lifecycle section keeps
  name/time/content, the cost claim is removed, and a new section renders
  `r.skill_loads` when present.
- `web.py`: `run_audit` builds the document (the web layer imports no IR
  today), the result gains `"skill_loads"`, and `status_line` is extended
  with evidence counts, e.g. `… , loads N (M costed, K unavailable),
  reloads R`. `fingerprint()` bumps **v5 → v6** (the cached result shape
  changes; `tests/test_web.py:52` asserts the constant and moves with
  it).
- `cli.py`: the document is always built and passed to `render_report`
  (today it is built only when `--ir-out` / `--ir-analyses` is given,
  `cli.py:109-114`); both flags keep serializing unchanged.

## Landing path ②: dispatch-based load detection

### Parser deltas (wire.py, items.py)

1. `_qoder_ref` captures `parent_tool_use_id` (previously dropped).
2. `load_session` loads `.meta.json` siblings of Qoder subagent
   transcripts; `Session` gains `subagent_meta: Dict[str, dict]` keyed
   `"subagent:<stem>"`, content kept as logged. Missing file → absent
   key; malformed JSON → skipped and counted (`meta_files_loaded`,
   degradation section).
3. `ContentItem` gains `tool_use_id`: set from the tool call payload's
   `id` and the tool result's `tool_call_id`. This also retires the
   documented FIFO simplification in `analyses.py` ("the IR carries no
   `tool_use_id`, so tool_call/tool_result pairing is a per-agent FIFO
   approximation"): `tool_result_without_call` /
   `tool_call_without_result` flags become id-exact.

### Dispatch

New dataclass, produced by `ir/dispatch.py`:

```
Dispatch:
  dispatch_id: str        # "<parent_agent_id>:dispatch:<n>"
  parent_agent_id: str
  tool_use_id: str        # the parent-side Agent tool call id
  tool_item_id: str | null     # matching parent ContentItem, when found
  subagent_agent_id: str | null
  brief_item_id: str | null
  brief_tokens_est: int | null
  subagent_type: str | null
  description: str | null
  meta: dict | null       # meta.json as logged (facts)
  phase_refs: list        # [{ref, kind, source, offset}]
  phase_id: str | null
```

**The join is the definition**: a dispatch exists when a subagent
transcript links to a parent tool_use_id by string equality — via
`parent_tool_use_id` on its records, or via meta.json `toolUseId` when
records lack the field (counted as `joined_via_meta_only`). The tool name
`Agent` is only an advisory signal for orphans:

- a parent `Agent` tool call with no matching subagent file → orphan
  dispatch (counted; a `Dispatch` with `subagent_agent_id: null`);
- a subagent file whose parent id matches no tool call → unmatched
  (counted);
- nested dispatch (a subagent itself calling `Agent`) is supported —
  `parent_agent_id` may be a subagent agent id.

Neither orphan direction is ever dropped silently, and a dispatch is
never fabricated from a tool name alone.

### Brief classification

The brief is the **first string-content user record** of a subagent-origin
agent. Classification:

- if it is a skill signature, the item keeps `kind: "skill_body"` (it is
  the body — tier A below) and is still the dispatch's `brief_item_id`;
- otherwise the item is re-classified as `kind: "dispatch_brief"`.

**D3 — decision, flagged visibly**: `dispatch_brief` is bucketed
`inject`, not `user` (a re-bucket, the only non-additive schema change in
1.1). Rationale: the parent spec's `user` bucket means real user text;
a dispatch brief is machine-authored — user-role by protocol, not by
authorship. Consumer consequence: anything that assumes user-bucket items
are `user_message` sees this item in `inject` as `dispatch_brief`.
Because bucket tallies are aggregated per call, the re-classification
must be applied before bucket aggregation (or trigger recomputation); the
invariant `Σbuckets + unattributed == anchor` and a recount test hold
either way.

### Phase recognition

Three tiers per dispatch, counted in coverage:

- **Tier A** — the brief itself is a skill body (signature). The phase is
  that skill (`phase_id` = its `skill_id`); recognition is FACT — the
  brief is the body. Tier A and tier B share the `phase_id` space; the
  coverage tier counts (`tierA` / `tierB` / `tierC`) keep a phase's
  recognition origin distinguishable, so a FACT phase is never conflated
  with a same-named rule match.
- **Tier B** — the brief text or the subagent's `Read` file paths match
  the rule set: each match yields a `phase_refs` entry
  `{ref, kind, source, offset}` with `kind ∈ {skill_file, phase_file,
  dir, unknown}`, `source` naming which text matched. `phase_id` is the
  matched rule's phase key.
- **Tier C** — no match: `phase_id: null`, `phase_refs: []`, counted.

The rule set lives in `ir/phase_rules.py` as versioned data
(`RULE_SET_VERSION` + patterns), **shipping empty**. Patterns are derived
only from calibration ③ and recorded with the corpus they came from —
never invented ahead of evidence. With an empty rule set, every
non-signature dispatch is tier C by construction.

### PhaseEntity and phase_id

`PhaseEntity {phase_id, name, observations: [...]}` where an observation
is `{kind, agent_id, dispatch_id | item_id, ts, tokens_est, channel}`:

- `kind: "dispatch"` — carries `brief_tokens_est` **only when the
  dispatch's phase refs resolve to exactly one phase**; multi-ref briefs
  carry `null` (no double counting across phases);
- `kind: "read"` — carries the phase file's `Read` item's EST.

`ContentItem` gains `phase_id` — a D1-style link: items matching a
recognized phase reference (read file path, dispatch brief) are stamped
with the phase id, and their bucket does not change.

### Coverage additions

`CoverageReport` gains three blocks (all re-derived counts, with the
corpus note and snapshot date stated alongside, per the parent spec's
discipline):

- `skill_load_evidence`: loads / with_body / unavailable / reloads /
  redundant_bodies / channels — `loads` counts `kind: "load"` rows and
  `reloads` counts `kind: "reload"` rows, so the two kinds are separately
  recountable; redundant bodies are counted without rows (per the join
  rule);
- `dispatch_links`: dispatches / joined / joined_via_meta_only /
  orphan_dispatches / unmatched_subagent_files / meta_files_loaded /
  briefs_found / briefs_missing;
- `phase_recognition`: tierA / tierB / tierC / rule_set_version (null
  until calibrated) / corpus_note.

## Landing path ③: calibration and baseline (gated)

Gated on the user supplying a real coding-v2 session. Constraints: local
processing only; the session (and any brief text quoted from it) is never
committed or uploaded.

Procedure (script/notebook one-off — not CI):

1. Build: `PYTHONPATH=src python3 -m agent_session_detective
   <transcript.jsonl> --ir-out out.json --ir-analyses analyses.json`.
2. Calibration pass over the document: list dispatches (`subagent_type`,
   `description`, meta `invocationName`); classify briefs (signature /
   phase-ref candidate / none); extract candidate references; list join
   residuals (orphans, unmatched files, missing briefs).
3. Derive phase patterns from candidates; write them into
   `ir/phase_rules.py` with a new `RULE_SET_VERSION` and a
   corpus fingerprint (session id, file count, snapshot date).
4. Re-run; review the loads table and coverage against the same evidence
   standard the earlier falsification reviews used — in particular whether
   reported magnitudes (the "dispatch-phase4a-coding 24K"-style numbers)
   are reproduced from items, not quoted.
5. Record the baseline note in `docs/`.

Never baseline from the retired 2026-07-09 sessions
(`0091bc97-318e-44a4-84f8-477c31fbfbee`,
`370896c5-9210-4b9e-a501-8f197a1171af`).

## Schema delta and versioning

`IR_VERSION` 1.0 → 1.1. Additive:

- `ContentItem.tool_use_id`, `ContentItem.phase_id`;
- `AuditDocument.skill_loads` / `dispatches` / `phases`;
- new item kind `dispatch_brief`;
- three `CoverageReport` blocks.

Non-additive (the one flagged change): **D3**, brief re-bucket
`user` → `inject`, with consumer-visible consequences stated above.
`BUCKETS` itself is unchanged. Golden fixtures move to `ir_version
"1.1"` where they assert it.

## Fact/est discipline additions

- **FACT**: item existence and sizes; tool call/result ids
  (`id` / `tool_call_id`); `parent_tool_use_id`; meta.json fields; brief
  text; `sha1` / `norm_sha1`; `wire_seq` / `gone_seq`.
- **DERIVED** (stated, checked rules): stub ↔ body pairing; reload vs
  redundant via liveness; the dispatch join by string equality.
- **EST**: `tokens_est` everywhere, including `cost_tokens_est`.
- **Never fabricated**: a load cost without a body item (`unavailable`,
  `cost_tokens_est: null`); a phase attribution without a rule match
  (`phase_id: null`); `rule_set_version` before calibration (null); a
  dispatch without a join (orphans are reported, not invented).

## Error handling and degradation

- Stub with no body ever: `unavailable`, counted — never a stub-derived
  number.
- Duplicate body carriers for one stub (e.g. attachment + user record):
  first wins per stub; the later body flows through the reload/redundant
  rule, so duplicates stay counted exactly once.
- Malformed/missing meta.json: absent key, counted
  (`meta_files_loaded`); join still works from `parent_tool_use_id` when
  records carry it.
- Subagent file with no parent link, or `Agent` call with no subagent
  file: counted in `dispatch_links` both directions; nothing crashes,
  nothing is dropped silently.
- Empty rule set / no phase match: tier C; phases and phase_id stay
  empty/null.
- Non-Qoder adapters: dispatch section empty (contract-shaped lists);
  the loads ledger runs off whatever `SkillEntity` observations exist;
  the marker-only render applies unconditionally.
- Missing timestamps: `ts: null`; `wire_seq` remains the ordering
  authority (the join is order-independent — it is id equality).

## Module placement

- `wire.py` — `_qoder_ref` 7th key; `load_session` meta loading;
  `Session.subagent_meta`.
- `ir/schema.py` — `LoadEvidence`, `Dispatch`, `PhaseEntity`, new kinds
  and fields, `IR_VERSION` 1.1.
- `ir/items.py` — retain tool ids; `phase_id` field.
- `ir/dispatch.py` (new) — join, brief classification (D3), orphan
  accounting, phase ref extraction.
- `ir/loads.py` (new) — the ledger; reads only the document.
- `ir/phase_rules.py` (new) — `RULE_SET_VERSION`, empty rule set.
- `ir/builder.py` — build order: items → spans → skills → dispatch
  (brief re-classification before bucket aggregation) → phase stamping →
  loads → coverage.
- `ir/analyses.py` — `skill_loads` rollups; dispatch rollups; id-exact
  tool pairing replaces FIFO.
- `ir/coverage.py` — three new blocks.
- `report.py` — evidence section + marker-only fix.
- `web.py` + `webapp/app.js` — document build, `skill_loads`, status
  line, marker-only fix, fingerprint v6.
- `cli.py` — always build the document; pass to `render_report`.

## Tests

1. **Ledger join** (`ir/loads.py` unit tests): stub+body pairing;
   first-body-wins with duplicate carriers; stub with no body →
   `unavailable` with `cost_tokens_est: null`; body-only first load;
   reload after compaction (`gone_seq` set); redundant count when the
   previous body is still alive (no row, no cost); channel recorded per
   body channel including `tool:read`.
2. **Render regressions (red-green, both surfaces)**: a stub-derived
   number never appears as a load cost — `report.py` lifecycle meta and
   `webapp/app.js` lifecycle line; the evidence table is the only place
   a cost number appears, labeled EST or `unavailable`.
3. **Dispatch join fixtures**: join via `parent_tool_use_id`; meta-only
   fallback counted; orphan in both directions; nested
   subagent→subagent; id-equality only (a format-parsing implementation
   must fail the fixture).
4. **Brief classification**: first string-content user record picked;
   signature first record keeps `skill_body` and is still
   `brief_item_id`; D3 — `dispatch_brief` lands in `inject`, and bucket
   tallies are recounted after re-classification.
5. **Phase tiers**: A/B/C fixtures with a fixture rule set and
   `rule_set_version` recorded; multi-ref brief → observation cost null;
   empty rule set → all non-signature dispatches tier C.
6. **Contract/property additions**: every `LoadEvidence` item reference
   resolves; `cost_tokens_est is null ⇔ cost_basis == "unavailable"`;
   coverage counters equal an independent recount; dispatch totals
   reconcile (`joined + joined_via_meta_only + orphan_dispatches ==
   len(dispatches)`); `Σbuckets + unattributed == anchor` still holds
   after D3.
7. **Fingerprint v6**: `tests/test_web.py` constant updated; cached
   results from v5 are invalidated by key change.
8. Existing suite updated where goldens move: `ir_version`
   `"1.1"`, marker-only lifecycle render.
