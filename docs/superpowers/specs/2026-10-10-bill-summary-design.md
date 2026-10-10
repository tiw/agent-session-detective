# Bill-Style Summary Design (P0-1)

## Goal

The web report currently opens with `status_line` and a summary card of raw
counts, then drops the reader into nine sections with no top-level
conclusion: the report *measures* but does not *answer*. P0-1 from the web
review: give the reader a bill — the first screen answers two questions
before any scrolling:

1. **成本账单** — "tokens 废在哪": the fixed set of waste/measurement
   categories with volumes, largest first.
2. **路由账单** — "该加载的加载了吗": expectation misses, judge positives,
   IF coverage — the routing-compliance verdict in one line.

The bill is a **synthesis surface**, not a new measurement: every number is
computed from blocks the report already carries (`token_stats`,
`skill_loads`, `expectations`, `judgments`, `if_results`, `billed_series`,
plus one new IR analysis). It also ships one genuinely new waste signal
locked in with the user: **subagent return-bloat detection** (subagent
returning process data instead of a distilled result).

## Decisions (locked with user)

1. **Dual bill**: cost + routing in one card; routing answers compliance,
   cost answers waste.
2. **Web-only**: no CLI surface change; `bill` lands in the web payload.
3. **Approach A — fixed categories**: bill rows are a fixed category list
   (not free-form top-N item mining), each linking to the existing section
   that carries the evidence. New module `bill.py`, pure function over
   already-serialized dicts.
4. **Subagent return-bloat detection is in scope** as a new analysis and a
   sixth cost row: per-dispatch return volume vs subagent process volume,
   bloated returns flagged.
5. **Bloat rule**: flag when `return_tokens_est >= 2000` (absolute) **or**
   the return is a verbatim echo of a subagent item (`norm_sha1` equality —
   fact-grade). Threshold is an uncalibrated initial value, a named
   constant, revisable after real sessions.
6. **Honesty rules carry over unchanged**: EST/observed numbers are never
   divided by a billed denominator; shares are computed only within the
   same vocabulary; no denominator → `share: null` → no percentage rendered.
   Billed totals (when `billed` mode is on) are shown as their own block,
   session-scoped, never blended into EST rows.

## Layering

```
ir/builder.py ──► document (AuditDocument)
                       │
                       ▼
ir/analyses.py::subagent_returns(document)      ← new analysis (fact layer)
                       │
web.py::run_audit ────┤  serializes everything the bill needs
                       ▼
bill.py::build_bill(token_stats, skill_loads,   ← new pure module
    subagent_returns, expectations, judgments,     (dicts in, dict out;
    if_results, billed_series, file_read_count,    no IR imports)
    adapter_id)
                       │
                       ▼
app.js::renderBill — card above the summary section
```

- `bill.py` imports nothing from `ir/` or `billing/`; it consumes the
  serialized blocks `run_audit` already assembles. This keeps the bill a
  presentation-layer synthesis and unit-testable with plain dict fixtures.
- `subagent_returns` lives in `ir/analyses.py` next to `skill_loads`
  because it needs the IR document (items + dispatches); it is serialized
  into the payload as its own top-level key (tree page can reuse it later).

## Cost rows (fixed six, sorted by volume desc; meta rows pinned last)

| key | label | volume | vocabulary | evidence grade |
|---|---|---|---|---|
| `context_growth` | 上下文增长 | `bucket_totals` sum, split attributed/residual; compaction count + source in detail | EST | 推断 (attribution coverage noted) |
| `repeat_tax` | 重复注入税 | `repeat_extra_tokens`, split 压缩恢复/其余 via `repeat_class_totals` | EST | 推断 |
| `skill_load_cost` | 技能加载成本 | ledger `cost_tokens_est` + loads/unavailable/reloads counts | EST | 证据 (load ledger) |
| `subagent_returns` | subagent 返回 | Σ `return_tokens_est` + flagged count | EST | 返回量=事实渠道; 臃肿判定=判断 |
| `output` | 输出 | `output_total` | observed or unavailable (mode) | 观测 |
| `cache_signal` | 缓存信号 | `cache_hit_rate` or billed cached share | meta row (rate, not volume) | 观测 |

Per-row rendering contract:

```json
{
  "key": "context_growth",
  "label": "上下文增长",
  "tokens_est": 123456,
  "share": 0.62,
  "detail": "attribution 71% · 压缩 2 次(transcript)",
  "anchor": "#tokens",
  "grade": "推断"
}
```

- `share` is computed only within the EST-vocabulary subtotal
  (context_growth + repeat_tax + skill_load_cost + subagent_returns);
  `output` (observed) and `cache_signal` never join that denominator.
- **Residual is always presented as its own whole row-split, never
  zero-filled**: the P0-2 finding (attachment-channel skill bodies land in
  the system residual in billed mode) means the row's detail must carry
  the attribution coverage note whenever residual dominates — the bill
  must never show "技能 0 tokens" style contradictions.
- No volume (e.g. no telemetry, no repeats) → row omitted, not zeroed.

## Routing bill (one line block)

```json
{
  "lines": [
    {"label": "expect 缺失", "value": "2/5", "grade": "判断", "anchor": "#expectations"},
    {"label": "judge 命中", "value": "3", "grade": "判断", "anchor": "#findings"},
    {"label": "IF 覆盖", "value": "0.87", "grade": "判断", "anchor": "#if"},
    {"label": "SKILL.md 直读", "value": "4 次", "grade": "事实", "anchor": "#loads"}
  ],
  "enabled": true
}
```

- The SKILL.md direct-read count comes from the caller as
  `file_read_count` (`len(timeline.file_reads)`); `build_bill` takes the
  count, not the timeline.
- Judge/IF disabled → those lines are omitted **and** one hint line is
  rendered ("judge 未启用，路由账单不完整"), never silently dropped.
- All lines off / no expectations → routing block renders the explicit
  "无路由信号" state.

## subagent_returns analysis (ir/analyses.py)

Per dispatch, from `document` only:

- `return_tokens_est` — the main-side `tool_result` item whose
  `tool_use_id == dispatch.tool_use_id` (`tokens_est`). Exists for orphan
  dispatches too (an Agent call always completes with a ToolResult), so
  the return side is measurable in **all** modes.
- `subagent_tokens_est` — Σ `tokens_est` of items with
  `agent_id == dispatch.subagent_agent_id`; `None` when the dispatch is an
  orphan (no join). kimi: always joined; Qoder: joined only with the
  IDE-DB side channel (`?ide_db=1` / `--ide-db`); Qoder CLI transcripts
  without the join degrade.
- `echo_item_id` — non-null when the return item's `norm_sha1` equals
  some subagent item's `norm_sha1`: the subagent forwarded process data
  verbatim. Fact-grade string equality, the same mechanism as duplicate
  detection.
- `ratio` — `return / subagent_tokens` only when both are measurable;
  `None` otherwise (no fabricated denominators).
- `flagged_reasons` — `["size"]` when `return_tokens_est >=
  RETURN_BLOAT_TOKENS` (constant in `ir/analyses.py`, 2000), `["echo"]`
  when `echo_item_id` is set, both when both fire.

Degradation honesty:

- Unlinked dispatches: return reported (it did enter main's context),
  process side labeled 不可用, bloat judged by the absolute threshold only.
- `totals`: dispatches, linked, `return_tokens_total`, flagged,
  `process_unavailable` — the counts the bill row cites.
- Session without dispatches → analysis returns empty rows; the bill row
  is omitted.

## Rendering (app.js)

- `renderBill(result.bill)` renders the card **above** the summary card;
  graceful skip when `bill` is absent (stale cache pre-v11).
- Section anchor ids are added to the existing sections the anchors
  reference (`#tokens`, `#loads`, `#expectations`, `#findings`, `#if` —
  ids only, no layout change).
- Subagent row expands inline: top-3 flagged dispatches, one line each
  (description, return vs process volume, reason). No new page.
- HELP gains a `bill` key explaining row vocabulary and the
  事实/观测/证据/推断 grades.

## Payload & cache

- `run_audit` adds two top-level result keys: `"bill"` (the block) and
  `"subagent_returns"` (analysis rows + totals).
- `status_line` is left unchanged in this change (the bill supersedes it
  visually; retiring it is a separate decision).
- Fingerprint **v10 → v11** (payload shape changed); `cache_key`
  unchanged (same params → same key, stale entries miss on fingerprint).

## Tests (tests/test_bill.py + subagent_returns cases in test_ir_analyses.py)

Bill (pure-function, dict fixtures):

1. kimi with telemetry — all six rows, shares sum to 1 within EST
   vocabulary, sorted desc, cache row pinned last.
2. kimi without telemetry — output row shows unavailable, no share.
3. Qoder + billed, low attribution — residual split present with coverage
   note; no "skill 0 tokens" style row; billed block separate.
4. Qoder without billed — no billed block, rows still render.
5. billed unavailable (`BillingUnavailable`) — absent billed block, note.
6. Routing: on / judge off (hint line) / all off (explicit empty state).
7. No dispatches — subagent row omitted.

subagent_returns:

8. kimi full join — return + process + ratio + echo hit.
9. Orphan dispatch — return valued, process `None`, size-only flagging.
10. Echo false-positive guard — similar but `norm_sha1` differs → no echo
    flag.
11. Threshold boundary — 1999 vs 2000 tokens.
12. No dispatches — empty rows/totals.

Existing 400+ tests untouched.
