# Calibrating the audit IR against Qoder's live /context view

Qoder's built-in context view (CLI/SDK `getContextUsage`, surfaced as
`/context`) reports the live composition of the context window:
category percentages for `system_prompt`, `tools`, `messages`, `skills`,
`memory` and `free_space`, per-skill `percentageOfContext`, and
`duplicateFileReads` with `wastedTokens`. It is snapshot-only and never
persisted — 0 hits across 25 real transcripts — so it cannot be
reconstructed after the fact and the offline IR never depends on it.

It is, however, a free calibration oracle for the audit IR's single
versioned estimator (`cjk-1.0`). A live session whose `/context`
snapshot was captured can be replayed offline to an `AuditDocument`,
and the two share vectors compared. This is a documented validation
procedure, not a runtime dependency: it is run by hand against real
sessions and never enters CI (the live side cannot be automated).

## Procedure

1. Run a live Qoder session and capture a `/context` snapshot (all six
   category percentages) for a request boundary you care about.
2. Project the same session offline:

   ```bash
   python3 scripts/calibrate_context.py ~/.qoder/projects/<workspace>/<session>.jsonl
   ```

   The transcript records `contextWindow` in its `runtime-config` record,
   so no window flag is needed for real sessions; `--window <tokens>`
   overrides the recorded window, and `--call <call_id>`
   projects a specific request instead of the default last one.
3. Compare the printed five-line share vector plus the
   `(unmapped buckets)` remainder against the snapshot, category by
   category.
4. Record the per-category deltas as the estimator's observed error
   bound for that session. Do not adjust the estimator from a single
   session; treat a repeated, consistent bias across sessions as a
   finding worth investigating.

## Mapping table

| Live category        | IR source                                                    |
| -------------------- | ------------------------------------------------------------ |
| `system_prompt`      | `unattributed` (together with `tools` — see below)            |
| `tools`              | `unattributed` (together with `system_prompt`)                |
| `messages`           | `user` + `assistant` + `tool` bucket sums                     |
| `skills`             | `skill` bucket sum                                            |
| `memory`             | sum of alive items with `kind == "memory"` inside `inject`    |
| `free_space`         | `1 - anchor_tokens / context_window`                          |

The offline IR observes neither `system_prompt` nor `tools` content, so
the live categories that do are not separable from it; the mapping
pairs them onto the one `unattributed` bucket. That bucket also absorbs
whatever else the anchor counts but no record surfaces (invisible
prefixes) plus the estimator's own error, so a mismatch there is not by
itself evidence of a bug.

The projected shares sum to **at most** 1.0, and exactly 1.0 when the
`system` and `tools` buckets are empty and `inject` holds nothing but
memory. Whatever the five live categories have no room for —
non-memory `inject` items (planning mode, reminders), `system`/`tools`
bucket items (MCP instruction deltas) — is deliberately left out of the
projection; the `(unmapped buckets)` row prints its share so the size
of the blind spot stays visible instead of being normalized away.

## Out of scope for v1

- Per-skill `percentageOfContext` (would need per-skill live values
  keyed the same way as `SkillEntity.skill_name`; the offline side
  already has per-skill token sums, so this is a v2 candidate).
- `duplicateFileReads` / `wastedTokens` (the live view's own redundancy
  report; the IR's redundancy analysis is structural, not token-based).

## Error bounds to expect

- Snapshot vs request: the live view is a point-in-time snapshot, while
  the IR projects a specific call boundary; capture them at the same
  boundary or expect drift.
- Unmapped buckets: non-memory `inject`, `system` and `tools` items are
  invisible to the live categories; their combined share prints in the
  `(unmapped buckets)` row.
- Both sides are estimates — Qoder's own tokenizer on the live side,
  `cjk-1.0` on the offline side — so agreement to a few percentage
  points is success, not exactness.
