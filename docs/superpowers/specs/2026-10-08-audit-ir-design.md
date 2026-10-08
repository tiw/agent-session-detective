# Agent Session Audit IR Design

## Goal

Give every agent session — from any harness — one auditable shape: a
**per-request content projection** that answers, with evidence, "what exactly
was in the prompt at each LLM call, where did each piece come from, and what
did it cost."

The IR must support three analyses the current event-stream model cannot
answer honestly:

1. **Skill audit** — every context footprint of every skill (catalog listing,
   body injection, stub marker, execution), counted per request, deduplicated
   by content identity, never absorbed into a generic "injected" bucket.
2. **Context organization** — per-request bucket composition (7 content
   buckets + explicit reconciliation), anchored against the provider-reported
   prompt total.
3. **Redundancy and useless content** — cross-bucket duplicate detection by
   content hash, re-injection counting, and born/gone liveness per item.

## Scope

In scope (v1):

- A standalone IR (JSON schema + Python dataclasses) and a builder that turns
  parsed wire records into the IR. The IR is consumable by other tools, not
  just ASD's own reports.
- **Qoder adapter first**, because its transcripts carry the richest
  telemetry. This requires two independent parser fixes in `wire.py`:
  - *Record-level*: relax the `type not in ("user", "assistant")` filter
    (wire.py:359) so `system`, `runtime-config`, `active-leaf`, and the 11
    `attachment` record types are retained.
  - *Field-level*: on already-retained assistant records, take
    `context_usage_ratio`, `request_id`, `credits`/`original_credits`/`billable`,
    `cache_creation.ephemeral_5m/1h_input_tokens`, `uuid`/`parentUuid`,
    and `isSidechain`.
- Kimi CLI/desktop and Codex adapters follow the same contract: schema now,
  implementation after Qoder.
- Three analyses (below) reading only the IR.
- A coverage report that states, per field, what is log fact, what is
  estimate, and what is missing.

Out of scope (v1):

- **No instrumentation.** Qoder transcripts already carry the required
  telemetry; skills self-reporting their own context share would violate the
  founding principle (coverage is re-derived, never self-declared).
- Session-tree branch replay (`active-leaf`): stored, not interpreted.
- Prompt-prefix hashes: Qoder logs none — the field is `null`, never
  fabricated.
- Web UI redesign: existing surfaces keep working; new analyses render
  through the existing report layer.

## Motivation: three structural flaws of the current model

1. **No first-class LLM-call object.** `Session` is a flat `Event` list; the
   per-request prompt is never materialized, so "what was in the prompt" is
   reverse-engineered from turn boundaries and status series.
2. **Buckets are computed, not observed.** `tokenstats.py:291` derives the
   system bucket as `max(0, growth − observed)` — a residual that launders
   projection error into a "system" number. The `injected` bucket conflates
   tool results, reminders, skill catalogs, and skill bodies.
3. **Content has no identity or lifecycle.** SHA-1 exists only for whole tool
   results ≥200 chars (`_repeats`); nothing has provenance, born/gone
   sequence, or cross-bucket dedup keys.

The IR fixes all three by making the projection itself the data: content is a
ledger of identified items; each request materializes references to the items
alive at that point, plus an anchor reconciliation.

## Schema

### AuditDocument (top level)

```
{
  ir_version: "1.0",
  generator: {name, version},          # ASD version
  adapter:   {id, version},            # "qoder" | "kimi-cli" | "kimi-desktop" | "codex"
  estimator_version: str,              # single versioned token estimator
  source_files: [path],
  agents:      [ContextAgent],
  requests:    [LLMCall],
  items:       [ContentItem],          # flat ledger — source of truth
  skills:      [SkillEntity],
  compactions: [Compaction],
  coverage:    CoverageReport,
}
```

### ContextAgent

```
{
  agent_id: "main" | <stable adapter id>,
  parent_id: str|null,
  origin_channel: str,          # e.g. "qoder:isSidechain" — identity from the
                                # record field, not directory-name guessing
  model: str|null,              # FACT (runtime-config)
  context_window: int|null,     # FACT (runtime-config; observed 128000/200000/272000)
  request_ids: [call_id],
  active_leaf: {leaf_uuid, explicit}|null,   # store-only in v1
}
```

Subagent contexts are first-class: each agent has its own `LLMCall` sequence
and its own item ledger slice; `SkillEntity` is shared across agents so a
skill's total footprint spans the tree. For Qoder, `agent_id` is a log fact:
records in the main transcript with `isSidechain: true` are attributed to
their sidechain agent via the uuid chain, and separate subagent transcript
files take their id from the subagent path — file placement is never the sole
evidence.

### LLMCall

```
{
  call_id: str,                # "<agent_id>:<request ordinal>"
  agent_id, wire_seq, ts, model,
  input: {
    item_refs: [{item_id, bucket, tokens_est}],   # materialized projection
    buckets: {system, tools, user, inject, skill, assistant, tool},  # tokens_est
    anchor_tokens: int|null,        # FACT: usage.input_tokens (provider prompt total)
    unattributed_tokens: int|null,  # anchor − Σ buckets, only when anchor present
    cache_read, cache_write_5m, cache_write_1h: int|null,   # FACT
    context_usage_ratio: float|null,   # FACT
    request_id: str|null,              # FACT
    credits: {credits, original_credits, billable}|null,    # FACT
    prefix_hashes: {system, tools}|null,   # null for Qoder — never fabricated
  },
  output: {
    parts: [{type: text|think|tool_call, item_id}],
    output_tokens: int,       # FACT
  },
}
```

### ContentItem

```
{
  item_id: str,               # stable ledger id
  agent_id: str,
  bucket: system|tools|user|inject|skill|assistant|tool,
  kind: <open enum>,          # system_prompt, tool_schema, user_message, reminder,
                              # memory, skill_catalog, skill_body, skill_stub,
                              # hook_inline, tool_result, assistant_text, thinking,
                              # compact_restore, unknown, ...
  name: str|null,             # skill name / file path / reminder label
  channel: str,               # namespaced, e.g. "qoder:attachment:skill_listing"
  wire_seq: int,              # born
  gone_seq: int|null,         # first request seq after the compaction that dropped it;
                              # null = alive to end of session
  size_chars: int,
  tokens_est: int,
  sha1: str,                  # content identity
  norm_sha1: str,             # identity after stripping volatile prefixes —
                              # cross-bucket dedup key
  record: {file, seq, uuid|null},   # evidence pointer back to the raw log
  preview: str,               # ≤200 chars — the IR stores identity, not bodies
  skill_id: str|null,         # D1: attribution link without re-bucketing
}
```

**Liveness** (the projection rule, portable from dsh-context via Qoder's
uuid/parentUuid chain):

```
alive_at(item, R) ⇔ item.wire_seq < R.wire_seq
                  ∧ (item.gone_seq = null ∨ R.wire_seq < item.gone_seq)
```

An assistant output item's own `wire_seq` naturally excludes it from its
producing request. `gone_seq` comes from the compaction boundary
(`compact_boundary` → items born before it are gone at every later request);
`post_compact_restored_files` items are born at the boundary.

### SkillEntity

```
{
  skill_id: str,              # normalized name
  name: str,
  observations: [{
    kind: listing|body|stub|execution,
    channel: str,             # where the footprint entered the context
    agent_id, call_id|null, ts,
    tokens_est: int,
    body_sha1: str|null,      # body observations only → version-drift detection
    item_id: str,
  }],
}
```

The four observation kinds make loading channels comparable: `listing` (the
skill catalog entry), `body` (full SKILL.md inline — via `invoked_skills`,
`hook_output`, or a tool `Read`), `stub` (the "Launching skill: X" marker,
~7–14 tokens), `execution` (tool activity attributed to the skill). The
stub-vs-body gap — the current pipeline under-reports skill loading by ~350× —
is itself a first-class audit finding.

### Compaction

```
{
  compaction_id, agent_id, ts, boundary_seq,
  trigger: auto|manual,                 # FACT
  pre_tokens, post_tokens: int|null,    # FACT (compactMetadata; e.g. 231067 → 3247)
  messages_summarized, duration_ms: int|null,   # FACT
  restored_item_ids: [item_id],         # from post_compact_restored_files
}
```

### CoverageReport

```
{
  per_field: {field: {fact: n, est: m, missing: k}},
  requests_with_anchor: "n/total",
  bucket_sources: {envelope: n, signature: m, envelope_vs_signature_conflicts: k},
  unknown_channels: [{channel, count, tokens_est}],
  dropped_records: {reason: count},
  notes: [str],
}
```

Coverage is computed by re-derivation (recounting from the IR itself), never
by trusting a model or harness self-report. Every number the report layer
shows must be traceable to a `record` evidence pointer or be labeled est.

## Bucket taxonomy and classification

Seven content buckets plus an explicit reconciliation bucket:

| bucket | contents | Qoder sources |
| --- | --- | --- |
| `system` | system prompt records | `system` subtype records |
| `tools` | tool schemas | no record exists → lands in unattributed (honest) |
| `user` | real user text | `user` records without attachments/tool results |
| `inject` | reminders, memory, goal/auto-mode state, file snapshots, restored files | `critical_system_reminder`, `task_reminder`, `goal_state`, `auto_mode`, `queued_command`, `edited_text_file`, `agent_listing_delta`, `post_compact_restored_files` |
| `skill` | skill catalogs and skill bodies | `skill_listing`, `invoked_skills`, `hook_output` when it inlines SKILL.md |
| `assistant` | assistant text and thinking | `assistant` records |
| `tool` | tool results | `user` records' `tool_result` parts |
| `unattributed` | reconciliation remainder, not a content bucket | `anchor_tokens − Σ buckets` |

Classification is **envelope-first**: the record's own typing (attachment
type, message role, part type) decides the bucket. A **content-signature
fallback** (dsh-context's approach: `<skill_content name="…">` wrapper regex,
reminder text signatures) classifies user records that carry
injection-shaped content without an envelope. Envelope wins on conflict;
conflicts are counted in coverage. Unknown record types become
`kind: "unknown"` items whose tokens land in unattributed — never guessed
into a bucket.

Two approved decisions:

- **D1 — SKILL.md read via the Read tool** stays in the `tool` bucket (it is
  a tool result; dsh-context's `categoryOf` does the same) and gains
  `skill_id`, producing a `body` observation with channel `tool:read`. The
  bucket answers *composition*; the skill ledger answers *attribution*.
  Without D1 the same content would flip buckets depending on which channel
  loaded it.
- **D2 — explicit `unattributed`** replaces the residual-system practice
  (tokenstats.py:291). The system bucket only contains observed system-prompt
  records; projection error is visible as unattributed, not laundered as
  "system".

## Fact/est discipline

- **FACT** (log or provider evidence only): `anchor_tokens`, `output_tokens`,
  cache fields, `context_usage_ratio`, `request_id`, `credits`, compaction
  `pre_tokens`/`post_tokens`/`trigger`, `context_window`, `model`,
  `wire_seq`/`gone_seq`, `size_chars`.
- **EST** (always from the single versioned estimator, always labeled):
  `tokens_est` everywhere, bucket sums, `unattributed_tokens` (derived:
  anchor − est), bucket shares.
- **Never fabricated**: `prefix_hashes` (null for Qoder), any field the
  source format does not record.

**Live calibration source** (from the 2026-10-08 due diligence): Qoder's
built-in live view — CLI/SDK `getContextUsage()`, surfaced as `/context` —
returns category percentages (`system_prompt`, `tools`, `messages`, `skills`,
`memory`, `free_space`), per-skill `percentageOfContext`, and
`duplicateFileReads` with `wastedTokens`. It is snapshot-only and never
persisted (0 hits across 25 real transcripts). It therefore does not compete
with this IR, but it is a free **estimator calibration oracle**: run a live
session, capture `/context` snapshots, run the offline IR reconstruction on
the same session, and compare bucket shares to bound estimator error. Mapping:
`system_prompt→system`, `tools→tools`, `messages→{user, assistant, tool}`,
`skills→skill`, `memory→inject(memory)`, `free_space→window remainder`. This
is a documented validation procedure, not a runtime dependency.

## Adapter contract

Every adapter produces the same `AuditDocument` shape. Harness differences
are absorbed by **namespaced channels** (`qoder:attachment:skill_listing`,
`kimi:source:skill-invocation`, `codex:item:*`) and by coverage notes — never
by schema forks. The same contract-assertion suite runs against every
adapter's fixture IR (see Tests). Adding a harness adds an adapter and
channels; it does not change the schema.

## Analyses

All three read only the IR — no re-parsing, no format-specific logic.

1. **Skill audit** (per SkillEntity): listing re-injection counts × catalog
   tokens (observed case: 37 injections of a 4,526-token catalog with only 2
   distinct versions ≈ 160K repeat tokens in one session); body channel
   distribution across `invoked_skills` / `hook_output` / `tool:read`;
   stub-vs-body gap; `body_sha1` version drift; body-without-execution flags
   (loaded but never ran → useless load, labeled inferred).
2. **Context organization** (per LLMCall): bucket matrix and share trends
   over the session; new-vs-re-injected ratio for inject+skill items (an
   item's `sha1` seen in an earlier request of the same agent =
   re-injection); unattributed share as a projection-quality alarm;
   compaction impact (fact pre/post tokens, alive spans, restored items).
3. **Redundancy and useless content**: cross-bucket `norm_sha1` groups —
   identical content surfacing in different buckets is true disorganization
   waste, which the current within-tool-result-only `_repeats` cannot see;
   uselessness heuristics (labeled inferred): tool result with no
   downstream correlated tool call within k requests; skill body without
   execution; item alive < 2 requests then compacted away.

## Error handling and degradation

- Malformed JSONL lines: skipped, counted in `dropped_records`.
- Unknown record/attachment types: `kind: "unknown"` item; tokens stay
  unattributed; channel + count + size listed in coverage.
- Sessions without an anchor (e.g. Kimi CLI without token telemetry):
  `unattributed_tokens: null`, bucket analyses degrade to est-only with a
  coverage note — never a zero-delta pseudo-fact.
- Missing timestamps: `ts: null`; `wire_seq` remains the ordering authority.
- Envelope/signature conflicts: envelope wins, conflict counted.
- Estimator failure on pathological content: `tokens_est: 0` plus a coverage
  note, not a crash.

## Module placement

- `src/agent_session_detective/ir/` — schema dataclasses, builder
  (wire records → AuditDocument), estimator version constant.
- `wire.py` — the two parser fixes above; the Event layer stays the parsing
  boundary, the builder is the projection boundary.
- Analyses extend the existing report layer, reading `AuditDocument`.
- Existing `tokenstats` residual bucket is superseded for IR consumers by the
  anchored `unattributed` (D2); the old path remains until report surfaces
  migrate.

## Tests

1. Per-format fixtures (Qoder first) with golden-IR assertions.
2. Property tests: liveness formula holds for every request×item pair;
   `Σbuckets + unattributed == anchor` whenever an anchor exists; every
   `item_id` referenced by a request exists in the ledger; every
   `skill_id` on an item resolves to a SkillEntity.
3. Adapter contract suite, run unchanged against each adapter's fixture IR.
4. Classification: envelope-first precedence over signatures; D1 (tool bucket
   + skill_id for a SKILL.md Read); unknown types land unattributed with
   coverage entries.
5. Compaction: `gone_seq` assignment, restored items born at the boundary,
   pre/post token facts surfaced.
6. Calibration procedure (scripted, not CI): one live Qoder session,
   `/context` snapshot vs offline IR reconstruction, error bounds recorded in
   the docs.
