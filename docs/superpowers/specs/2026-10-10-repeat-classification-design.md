# Repeat Injection Classification Design

## Goal

ASD's repeat-injection detector currently labels every repeated large tool
result as "状态未外置的可见信号". Verification against session b1d65022
showed that attribution is wrong: 99.6% of the 23,709 extra tokens were
constitution-protocol-legal post-compaction re-reads; the rest were one
polling false positive (GetTerminalOutput, ~90 tokens) and one genuinely
avoidable re-read (~5k). This change teaches the detector **why** each
repetition happened, so the page distinguishes legal recovery from actual
waste instead of lumping them into one number with one caption.

Evidence sources for compaction points (two, merged):

- **Transcript** (free, always on): rich-format sessions carry
  `CompactionBegin` events → `timeline.compactions` with `begin_ts`.
  CLI-shaped sessions (like b1d65022) have none.
- **Billed sawtooth** (opt-in): SharedClientCache billed series — prompt
  drops >25% with cached→0 mark a compaction between two billed rows.
  Follows the established constraint: external live data sources default
  opt-in; the transcript stays the default-only fact source.

## Decisions (locked with user)

1. **Dual compaction sources; billed stays opt-in.** Transcript compaction
   telemetry is used whenever present; the SharedClientCache series is
   consulted only under the existing `--billed-usage` flag.
2. **No evidence source → neutral presentation.** Repeats render with
   `unclassified` and a neutral caption ("无压缩证据源，未分类"); no
   fabricated classes, no zero-delta pseudo-data.
3. **Poll detection = combined signature.** Same tool + byte-identical
   output + gap ≤120s + single output <2k chars. Compaction classification
   takes precedence over poll.
4. **All three surfaces updated**: session page, markdown report, fleet
   rollup badge.

## Layering

The tokenstats layer stays sqlite-free; billing stays timeline-free.

```
billing.py                     tokenstats.py                 caller (cli.py / web.py)
──────────                     ─────────────                 ───────────────────────
query_billed_series ──► rows   merge + classify(occurrences, compaction_windows)
compaction_points_from_series  ◄────────────────────────────┐
                              returns Repeat w/ classes      timeline.compactions
                                │                            (transcript points)
                                ▼
                        TokenStats.repeats / repeat_class_totals
```

Compaction windows enter `build_token_stats` as **plain data**
(`List[dict]`); the caller merges transcript points with billed points.
Tokenstats never imports sqlite or timeline.

## Data model (tokenstats.py)

`Repeat` gains fields (existing ones unchanged):

| field | type | meaning |
|---|---|---|
| `tool_name` | `Optional[str]` | paired ToolCall function name (poll signature) |
| `chars_each` | `int` | per-occurrence output length (<2k poll test) |
| `occurrence_ts` | `List[Optional[float]]` | per-occurrence timestamps |
| `occurrence_classes` | `List[str]` | same length; first = `"first"`, rest in `{post_compaction, poll, no_compaction, unclassified}` |
| `extra_by_class` | `Dict[str, int]` | class → (count of occurrences ≥2 in class) × tokens_each |

`TokenStats` gains: `repeat_class_totals: Dict[str, int]` (summed
extra_by_class), `compaction_source: Optional[str]` (`"transcript"` /
`"billed"` / `"transcript+billed"` / `None`), `compaction_points:
List[dict]` (evidence blocks for the report).

Headline `repeat_extra_tokens` stays the TOTAL (backward compatible; the
fleet ≥5000 threshold keeps reading the total).

## Classification rules

A compaction window is a half-open-ish interval: billed source gives
`[window_start, window_end]` = `[prev_billed.ts, drop.ts]` (same-second
re-reads count via overlap — verified against all three b1d65022
compactions); transcript source degrades to a point `[ts, ts]`.

For occurrence i ≥ 1, in priority order:

1. **post_compaction** — some window overlaps the gap
   `(occurrence_ts[i-1], occurrence_ts[i]]`, i.e.
   `w.start < occ[i] and w.end > occ[i-1]`. `None` timestamps never
   classify as post_compaction.
2. **poll** — not post_compaction, and combined signature holds:
   same tool + byte-identical output + gap ≤120s + `chars_each < 2000`.
3. **no_compaction** — a source is available (`compaction_source` set) and
   no window overlaps the gap. This is the **true violation signal**.
4. **unclassified** — no source at all.

`extra_by_class[c]` = (number of occurrences with index ≥1 classified c)
× tokens_each. The first occurrence never contributes to extra.
`compaction_source is None` ⇒ everything lands in unclassified — zero
misclassification when evidence is absent.

## billing.py additions

Two functions alongside the existing `query_billed_usage`:

- `query_billed_series(session_uuid, db_path=None) -> List[dict]` —
  `SELECT gmt_create, token_info FROM chat_message WHERE session_id=?
  ORDER BY gmt_create`; parse each `token_info` JSON; return rows
  `[{"ts", "prompt", "completion", "cached", ...}]`. The `gmt_create`
  unit/format is verified by the first TDD test (expected ms-epoch).
- `compaction_points_from_series(rows) -> List[dict]` — scan consecutive
  pairs; prompt drop >25% **and** cached→0 →
  `{"ts", "window_start", "window_end", "pre_prompt", "post_prompt"}`.
  Gradual decline must not fire (both conditions required).

Failure semantics inherit the established degrade rules: DB missing /
locked / table missing / bad JSON → the caller degrades to
transcript-only classification and appends a coverage note; nothing
raises.

## Caller wiring

- **CLI**: the existing `--billed-usage` / `--billed-db` flags already
  gate SharedClientCache access. When on, the CLI additionally queries the
  series and derives billed compaction points; failure degrades to
  transcript-only with a note. No new flags.
- **Audit job (web)**: `Job` gains a `billed` param mirroring the tree
  page's `?billed=1` pattern (no db-path over HTTP; tests monkeypatch
  `DEFAULT_DB_PATH`). Off ⇒ pure transcript, byte-identical behavior.
- **Fleet rollup**: stays transcript-only — no per-session DB opens.

## Surfaces

- **Session page** (web.py serialization + app.js):
  - Repeats table gains per-group class badges, e.g.
    `压缩恢复 ×2 · 轮询 ×1`.
  - Caption becomes dynamic: with a source — split statement ("其中压缩
    恢复 X token 属压缩恢复协议内合法重读，其余 Y 才是重读税"); without a
    source — neutral ("无压缩证据源，未分类；--billed-usage 可接入账单证
    据").
  - Evidence line: compaction point count + source provenance.
- **Report** (report.py, both repeats blocks): classification column plus
  the lower-bound note ("被编辑文件（如 progress.md）内容每次不同，不构
  成重复组——extra 总量是重读税下界").
- **Fleet**: badge split `repeat +X (压缩恢复 Y / 其它 Z)`; threshold
  semantics unchanged (reads the total).

## Non-goals

- No IR change: repeats have never been part of the AuditDocument; no
  `IR_VERSION` bump, no schema/builder/analyses edits.
- No per-occurrence cost re-modeling: EST numbers, buckets, and the
  degrade-banner logic are untouched.
- No fleet-side DB access.
- No retroactive editing of the old caption's wording elsewhere.

## Testing (TDD)

- `tests/test_tokenstats.py` (new — `_repeats` has zero direct tests
  today): grouping of multi-occurrence outputs; classification under
  transcript windows; overlap semantics for same-second re-reads; poll
  signature hit and miss; no-source → all unclassified; `extra_by_class`
  arithmetic; headline total unchanged.
- `tests/test_billing.py` (append): `query_billed_series` against a tmp
  sqlite fixture (order, ts unit); `compaction_points_from_series`
  hits on drop>25%+cached→0, no false positive on gradual decline, and
  empty series → empty list.
- `tests/test_cli.py` (append): billed flag on → classification present;
  off → classes all `unclassified` / source `None`.
- `tests/test_web.py` (append): serialization carries the new fields;
  audit-job billed param; fleet badge split fields.

## Known consequences

- `web.py` source changes invalidate the judge cache fingerprint (v7) —
  expected; cross-fingerprint comparisons are already not supported.
- Old verdict text ("状态未外置的可见信号") disappears from new reports;
  existing saved reports keep their frozen HTML.

## Concurrency discipline (shared worktree)

1. `git status --porcelain` shows no tracked-file modifications before
   starting; pre-existing red baseline → stop and wait for the user.
2. Stage only task-owned files by explicit path; never `git add -A`; no
   push without explicit user request.
3. Foreign untracked files are never staged.
