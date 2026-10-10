# Repeat-Injection Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify every repeat-injection occurrence as post_compaction / poll / no_compaction / unclassified, using transcript compaction records plus an opt-in billed-sawtooth side channel as compaction evidence, and surface the split in the report, the webapp, the fleet badge, and next-step suggestions.

**Architecture:** The tokenstats layer gains a classification pass over the existing sha1-grouped repeat detector. Compaction evidence arrives as plain `[{ts, window_start, window_end, ...}]` windows merged from two sources (transcript `timeline.compactions`; billed sawtooth from SharedClientCache behind opt-in flags), so tokenstats stays sqlite-free and billing stays timeline-free. The web payload, HTML report, webapp UI and suggestion engine consume the new fields; the judge-cache fingerprint bumps to v9 because the payload shape changes (v8 is taken: commit 12c0b97 folded `ir<IR_VERSION>` into the key, so v9 is a web-local payload bump that keeps the `:ir%s` suffix).

**Tech Stack:** Python 3 stdlib only (unittest, sqlite3, json, hashlib, dataclasses); pytest as runner; plain-JS webapp (`webapp/app.js`).

---

## Conventions

- Repo root: `/Users/wangting/work/agent-session-detective`. All paths below are relative to it.
- Run tests from repo root: `PYTHONPATH=src python3 -m pytest -q tests/<file>.py`.
- Tests are stdlib `unittest.TestCase`. No new dependencies.
- **Token arithmetic:** `estimate_tokens(text)` (timeline.py:25) counts CJK chars as ~1 token each and other chars at 4 chars/token: `"x" * 300` → **75 tokens**; a 286-char ASCII body → 71 tokens. Test expectations below use these exact numbers.
- **Staging discipline (shared worktree):** run `git status` before every commit; stage ONLY the files the task lists, by explicit path; never `git add -A` or `git add .`; never push; never touch `tests/test_ir_intervention.py` or `.changes/`.
- **External-data honesty:** the SharedClientCache DB is read only behind opt-in flags (CLI `--billed-usage`, web `billed: true`). Any DB problem becomes an `unavailable (reason)` note — never a guess, never a raise out of the audit path.
- **No temp files in the repo:** live verification (Task 9) writes nothing into the workspace; sweep the repo root for stray files before closing Task 10.

## Design notes (spec deltas an implementer must keep)

Spec: `docs/superpowers/specs/2026-10-10-repeat-classification-design.md` (commit 337c1f7).

Two deliberate refinements were locked while turning the spec into code:

1. Transcript compaction windows are `[begin_ts, end_ts or begin_ts]`, not the spec's point windows `[ts, ts]` — same strict-overlap formula, sharper edges.
2. `_repeats` returns a **3-tuple** `(top10_repeats, total_over_ALL_groups, class_totals_over_ALL_groups)` so per-class totals always add up against the headline `repeat_extra_tokens` (the fleet badge splits that exact total).

Locked decisions: dual-source windows with the billed side opt-in; no evidence source → neutral caption + `unclassified` (never a silent "violation" claim); poll signature = same tool + gap ≤120s + each copy <2000 chars; classification priority `post_compaction` > `poll` > `no_compaction` > `unclassified`; occurrence 0 is always "first" and never contributes extra tokens.

---

### Task 1: `query_billed_series` — the billed time series

**Files:**
- Modify: `src/agent_session_detective/billing.py` (import line ~14; insert two functions between `session_uuid_from_source` and `query_billed_usage`)
- Test: `tests/test_billing.py` (extend the import block; append a helper + a test class before the `if __name__ == "__main__":` block at the end of the file)

- [ ] **Step 1: Write the failing tests**

In `tests/test_billing.py`, extend the billing import block:

```python
from agent_session_detective.billing import (
    BillingUnavailable,
    attach_billed_usage,
    query_billed_series,
    query_billed_usage,
    session_uuid_from_source,
)
```

Then insert the following immediately BEFORE the final `if __name__ == "__main__":` / `unittest.main()` lines:

```python
def make_series_db(path: Path, rows) -> Path:
    """3-column chat_message (IR 1.5+): gmt_create is INTEGER epoch ms."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chat_message "
        "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
    for gmt, info in rows:
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)",
                     (UUID, gmt, None if info is None else json.dumps(info)))
    conn.commit()
    conn.close()
    return path


class QueryBilledSeriesTest(unittest.TestCase):
    def test_series_rows_are_ts_sorted_in_seconds(self):
        # gmt_create is INTEGER epoch milliseconds; a value below 1e11 is
        # already seconds. Rows come back time-sorted with float seconds.
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (1754709642658, {"prompt_tokens": 50000, "completion_tokens": 40,
                                 "cached_tokens": 0}),
                (1754709632658, {"prompt_tokens": 100000, "completion_tokens": 40,
                                 "cached_tokens": 80000}),
            ])
            rows = query_billed_series(UUID, db_path=db)
            self.assertEqual([r["ts"] for r in rows],
                             [1754709632.658, 1754709642.658])
            self.assertEqual(rows[0]["prompt"], 100000)
            self.assertEqual(rows[0]["cached"], 80000)

    def test_malformed_rows_are_skipped_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (0, {"prompt_tokens": 1}),
                (1754709632658, None),
                (1754709642658, {"prompt_tokens": "x"}),
                (1754709652658, {"prompt_tokens": 20000, "completion_tokens": 5,
                                 "cached_tokens": 0}),
            ])
            rows = query_billed_series(UUID, db_path=db)
            self.assertEqual([r["ts"] for r in rows], [1754709652.658])

    def test_missing_db_raises_billing_unavailable(self):
        with self.assertRaises(BillingUnavailable):
            query_billed_series(UUID, db_path=Path("/nonexistent/asd-test.db"))

    def test_zero_parseable_rows_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (1754709632658, {"prompt_tokens": "x"}),
            ])
            with self.assertRaises(BillingUnavailable):
                query_billed_series(UUID, db_path=db)
```

Note: the pre-existing 2-column `make_db` helper and its tests stay untouched; the series reader must work on the 3-column layout because it selects `gmt_create` too.

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_billing.py`
Expected: FAIL/ERROR with `ImportError: cannot import name 'query_billed_series' from 'agent_session_detective.billing'` (the four new tests error at collection of the import; all pre-existing tests pass).

- [ ] **Step 3: Implement**

In `src/agent_session_detective/billing.py`:

Change the typing import (top of file) from `from typing import Optional` to:

```python
from typing import List, Optional
```

Insert immediately before the existing `def query_billed_usage(` line:

```python
def _gmt_create_to_ts(value) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    # chat_message.gmt_create is INTEGER epoch milliseconds; a value below
    # 1e11 is already seconds.
    return n if n < 1e11 else n / 1000.0


def query_billed_series(session_uuid: str, db_path=None) -> List[dict]:
    """Billed token rows for one session, time-sorted:
    [{ts, prompt, completion, cached}, ...]."""
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise BillingUnavailable("db not found: %s" % path)
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BillingUnavailable(str(exc)) from exc
    rows: List[dict] = []
    try:
        cursor = conn.execute(
            "SELECT gmt_create, token_info FROM chat_message "
            "WHERE session_id = ? ORDER BY gmt_create", (session_uuid,))
        while True:
            batch = cursor.fetchmany(200)
            if not batch:
                break
            for gmt, raw in batch:
                ts = _gmt_create_to_ts(gmt)
                if ts is None:
                    continue
                try:
                    info = json.loads(raw)
                    if not isinstance(info, dict):
                        raise ValueError("token_info is not an object")
                    rows.append({"ts": ts,
                                 "prompt": int(info.get("prompt_tokens") or 0),
                                 "completion": int(info.get("completion_tokens") or 0),
                                 "cached": int(info.get("cached_tokens") or 0)})
                except (TypeError, ValueError, KeyError):
                    continue
    except sqlite3.Error as exc:
        raise BillingUnavailable(str(exc)) from exc
    finally:
        conn.close()
    if not rows:
        raise BillingUnavailable(
            "no parseable billed rows for session %s" % session_uuid)
    return rows
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_billing.py`
Expected: all pass (pre-existing tests + 4 new).

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/billing.py tests/test_billing.py
git commit -m "feat: add query_billed_series to the billing side-channel"
```

---

### Task 2: Compaction points from the billed sawtooth

**Files:**
- Modify: `src/agent_session_detective/billing.py` (typing import; append constants + two functions at end of file, after `attach_billed_usage`)
- Test: `tests/test_billing.py` (extend the import block; append a test class before `if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_billing.py`, the billing import block becomes:

```python
from agent_session_detective.billing import (
    BillingUnavailable,
    attach_billed_usage,
    billed_compaction_points,
    compaction_points_from_series,
    query_billed_series,
    query_billed_usage,
    session_uuid_from_source,
)
```

Insert immediately BEFORE the final `if __name__ == "__main__":` lines:

```python
class CompactionPointsTest(unittest.TestCase):
    def test_sawtooth_fires_a_compaction_point(self):
        points = compaction_points_from_series([
            {"ts": 100.0, "prompt": 100000, "completion": 40, "cached": 80000},
            {"ts": 110.0, "prompt": 50000, "completion": 40, "cached": 0},
        ])
        self.assertEqual(points, [
            {"ts": 110.0, "window_start": 100.0, "window_end": 110.0,
             "pre_prompt": 100000, "post_prompt": 50000}])

    def test_gradual_decline_does_not_fire(self):
        points = compaction_points_from_series([
            {"ts": 100.0, "prompt": 100000, "completion": 40, "cached": 80000},
            {"ts": 110.0, "prompt": 90000, "completion": 40, "cached": 0},
        ])
        self.assertEqual(points, [])

    def test_cached_not_zero_does_not_fire(self):
        points = compaction_points_from_series([
            {"ts": 100.0, "prompt": 100000, "completion": 40, "cached": 80000},
            {"ts": 110.0, "prompt": 50000, "completion": 40, "cached": 40000},
        ])
        self.assertEqual(points, [])

    def test_empty_or_single_row_has_no_points(self):
        self.assertEqual(compaction_points_from_series([]), [])
        self.assertEqual(compaction_points_from_series(
            [{"ts": 100.0, "prompt": 100000, "completion": 40, "cached": 80000}]),
            [])

    def test_billed_compaction_points_reports_unavailable_reason(self):
        points, reason = billed_compaction_points(
            UUID, db_path=Path("/nonexistent/asd-test.db"))
        self.assertEqual(points, [])
        self.assertIn("db not found", reason)

    def test_billed_compaction_points_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (1754709632658, {"prompt_tokens": 100000, "completion_tokens": 40,
                                 "cached_tokens": 80000}),
                (1754709642658, {"prompt_tokens": 50000, "completion_tokens": 40,
                                 "cached_tokens": 0}),
            ])
            points, reason = billed_compaction_points(UUID, db_path=db)
            self.assertIsNone(reason)
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0]["post_prompt"], 50000)
            self.assertEqual(points[0]["window_start"], 1754709632.658)
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_billing.py`
Expected: FAIL/ERROR with `ImportError: cannot import name 'billed_compaction_points'`.

- [ ] **Step 3: Implement**

In `src/agent_session_detective/billing.py`, change the typing import to:

```python
from typing import List, Optional, Tuple
```

Append at the very end of the file (after `attach_billed_usage`):

```python
# A compaction shows up in the bill as a hard prompt drop (fresh context
# below 75% of the previous request) with a cold cache (cached -> 0).
COMPACTION_DROP_RATIO = 0.75


def compaction_points_from_series(rows: List[dict]) -> List[dict]:
    points: List[dict] = []
    for prev, cur in zip(rows, rows[1:]):
        if (prev["prompt"] > 0
                and cur["prompt"] < prev["prompt"] * COMPACTION_DROP_RATIO
                and cur["cached"] == 0):
            points.append({"ts": cur["ts"], "window_start": prev["ts"],
                           "window_end": cur["ts"], "pre_prompt": prev["prompt"],
                           "post_prompt": cur["prompt"]})
    return points


def billed_compaction_points(session_uuid: str,
                             db_path=None) -> Tuple[List[dict], Optional[str]]:
    """Compaction points from the billed series. Never raises: any billing
    problem returns ([], reason)."""
    try:
        rows = query_billed_series(session_uuid, db_path=db_path)
    except BillingUnavailable as exc:
        return [], exc.reason
    return compaction_points_from_series(rows), None
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_billing.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/billing.py tests/test_billing.py
git commit -m "feat: derive compaction points from the billed series"
```

---

### Task 3: Classification core in tokenstats

**Files:**
- Create: `tests/test_tokenstats.py`
- Modify: `src/agent_session_detective/tokenstats.py` (constants after `MIN_REPEAT_CHARS` ~line 29; `Repeat` fields after `extra_tokens` ~line 101; `TokenStats.repeat_class_totals` after `repeat_extra_tokens` ~line 119; new helpers + `_repeats` replacement ~lines 413-443; call site in `build_token_stats` line 463)

- [ ] **Step 1: Write the failing tests (new file)**

Create `tests/test_tokenstats.py` with exactly:

```python
"""Repeat-injection classification: per-occurrence classes and totals."""

import unittest
from pathlib import Path

from agent_session_detective.tokenstats import (
    _classify_occurrences,
    _extra_by_class,
    _repeats,
)
from agent_session_detective.wire import Event

BIG = "x" * 300  # estimate_tokens -> 75 (ASCII, 4 chars per token)
SESSION = Path("/tmp/asd-test.jsonl")


def tool_call(call_id, name, ts, seq):
    return Event(ts=ts, type="ToolCall", origin="main", source=SESSION, seq=seq,
                 payload={"id": call_id,
                          "function": {"name": name, "arguments": "{}"}})


def tool_result(call_id, output, ts, seq):
    return Event(ts=ts, type="ToolResult", origin="main", source=SESSION, seq=seq,
                 payload={"tool_call_id": call_id,
                          "return_value": {"output": output}})


def poll_pair_events():
    return [tool_call("c1", "Read", 10.0, 1), tool_result("c1", BIG, 10.0, 2),
            tool_call("c2", "Read", 40.0, 3), tool_result("c2", BIG, 40.0, 4)]


class ClassifyOccurrencesTest(unittest.TestCase):
    WINDOWS = [{"window_start": 5.0, "window_end": 50.0}]

    def test_first_occurrence_is_never_classified(self):
        self.assertEqual(
            _classify_occurrences([10.0], {0: "Read"}, 300, self.WINDOWS),
            ["first"])

    def test_without_windows_late_pairs_are_unclassified(self):
        classes = _classify_occurrences([10.0, 900.0], {0: "Read", 1: "Grep"},
                                        300, [])
        self.assertEqual(classes, ["first", "unclassified"])

    def test_window_overlap_is_post_compaction(self):
        classes = _classify_occurrences([10.0, 20.0], {0: "Read", 1: "Read"},
                                        300, self.WINDOWS)
        self.assertEqual(classes, ["first", "post_compaction"])

    def test_occurrence_at_window_end_still_overlaps(self):
        classes = _classify_occurrences([10.0, 50.0], {0: "Read", 1: "Read"},
                                        300, self.WINDOWS)
        self.assertEqual(classes, ["first", "post_compaction"])

    def test_missing_timestamps_are_unclassified(self):
        classes = _classify_occurrences([10.0, None], {0: "Read", 1: "Read"},
                                        300, self.WINDOWS)
        self.assertEqual(classes, ["first", "unclassified"])

    def test_compaction_wins_over_poll(self):
        # Same tool, 30s gap, small output: a poll signature — but the pair
        # sits inside a compaction window, and post_compaction wins.
        classes = _classify_occurrences([10.0, 40.0], {0: "Read", 1: "Read"},
                                        300, self.WINDOWS)
        self.assertEqual(classes, ["first", "post_compaction"])

    def test_poll_signature(self):
        classes = _classify_occurrences([10.0, 40.0], {0: "Read", 1: "Read"},
                                        300, [])
        self.assertEqual(classes, ["first", "poll"])

    def test_gap_exactly_120s_is_poll(self):
        classes = _classify_occurrences([10.0, 130.0], {0: "Read", 1: "Read"},
                                        300, [])
        self.assertEqual(classes, ["first", "poll"])

    def test_gap_200s_is_not_poll(self):
        classes = _classify_occurrences([10.0, 210.0], {0: "Read", 1: "Read"},
                                        300, [])
        self.assertEqual(classes, ["first", "unclassified"])

    def test_chars_2000_is_not_poll(self):
        classes = _classify_occurrences([10.0, 40.0], {0: "Read", 1: "Read"},
                                        2000, [])
        self.assertEqual(classes, ["first", "unclassified"])

    def test_different_tools_is_not_poll(self):
        classes = _classify_occurrences([10.0, 40.0], {0: "Read", 1: "Grep"},
                                        300, [])
        self.assertEqual(classes, ["first", "unclassified"])

    def test_no_compaction_requires_a_source(self):
        # With an evidence source present but no overlapping window, a late
        # large pair is the genuine violation signal: no_compaction.
        classes = _classify_occurrences([10.0, 900.0], {0: "Read", 1: "Read"},
                                        300,
                                        [{"window_start": 1000.0,
                                          "window_end": 1001.0}])
        self.assertEqual(classes, ["first", "no_compaction"])


class ExtraByClassTest(unittest.TestCase):
    def test_first_never_contributes(self):
        counts = _extra_by_class(
            ["first", "post_compaction", "poll", "post_compaction"], 100)
        self.assertEqual(counts, {"post_compaction": 200, "poll": 100})


class RepeatsTest(unittest.TestCase):
    def test_single_occurrence_is_ignored(self):
        self.assertEqual(_repeats(poll_pair_events()[:2]), ([], 0, {}))

    def test_small_outputs_are_ignored(self):
        events = [tool_call("c1", "Read", 10.0, 1),
                  tool_result("c1", "tiny", 10.0, 2),
                  tool_call("c2", "Read", 40.0, 3),
                  tool_result("c2", "tiny", 40.0, 4)]
        self.assertEqual(_repeats(events), ([], 0, {}))

    def test_without_windows_repeats_are_unclassified(self):
        events = [tool_call("c1", "Read", 10.0, 1),
                  tool_result("c1", BIG, 10.0, 2),
                  tool_call("c2", "Grep", 900.0, 3),
                  tool_result("c2", BIG, 900.0, 4)]
        repeats, total, class_totals = _repeats(events)
        self.assertEqual(repeats[0].occurrence_classes,
                         ["first", "unclassified"])
        self.assertEqual(class_totals, {"unclassified": 75})
        self.assertEqual(total, 75)

    def test_transcript_window_makes_repeats_post_compaction(self):
        windows = [{"window_start": 5.0, "window_end": 50.0}]
        repeats, total, class_totals = _repeats(poll_pair_events(), windows)
        self.assertEqual(repeats[0].occurrence_classes,
                         ["first", "post_compaction"])
        self.assertEqual(class_totals, {"post_compaction": 75})
        self.assertEqual(total, 75)

    def test_poll_pair_end_to_end(self):
        repeats, total, class_totals = _repeats(poll_pair_events(), [])
        self.assertEqual(repeats[0].occurrence_classes, ["first", "poll"])
        self.assertEqual(repeats[0].tool_name, "Read")
        self.assertEqual(class_totals, {"poll": 75})
        self.assertEqual(total, 75)

    def test_class_totals_count_all_groups_not_top10(self):
        # 12 distinct groups (bodies differ, zero-padded so lengths match);
        # 1000s gaps so nothing is poll; no windows so unclassified. Top-10
        # keeps 10 rows, but totals must cover ALL 12 groups.
        events = []
        seq = 0
        for i in range(12):
            body = "g%02d %s" % (i, BIG[:280])  # 286 chars -> 71 tokens
            base = 10.0 + i * 1000.0
            seq += 1
            events.append(tool_call("c%d" % i, "Read", base, seq))
            seq += 1
            events.append(tool_result("c%d" % i, body, base, seq))
            seq += 1
            events.append(tool_call("c%d" % i, "Read", base + 1000.0, seq))
            seq += 1
            events.append(tool_result("c%d" % i, body, base + 1000.0, seq))
        repeats, total, class_totals = _repeats(events, [])
        self.assertEqual(len(repeats), 10)
        self.assertEqual(total, class_totals["unclassified"])
        self.assertGreater(total, sum(r.extra_tokens for r in repeats))

    def test_mixed_tools_yield_no_tool_name(self):
        events = [tool_call("c1", "Read", 10.0, 1),
                  tool_result("c1", BIG, 10.0, 2),
                  tool_call("c2", "Grep", 900.0, 3),
                  tool_result("c2", BIG, 900.0, 4)]
        repeats, _, _ = _repeats(events, [])
        self.assertIsNone(repeats[0].tool_name)
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py`
Expected: FAIL/ERROR with `ImportError: cannot import name '_classify_occurrences' from 'agent_session_detective.tokenstats'`.

- [ ] **Step 3: Implement in tokenstats.py**

3a. Immediately after the existing `MIN_REPEAT_CHARS = 200` constant (~line 29), add:

```python
# A repeat pair looks like polling when the same tool re-returns similar
# content within two minutes and each copy is small; large outputs re-read
# after minutes are not polling.
REPEAT_POLL_GAP_S = 120
REPEAT_POLL_MAX_CHARS = 2000
```

3b. Extend the `Repeat` dataclass (after its existing `extra_tokens` field):

```python
    tool_name: Optional[str] = None
    chars_each: int = 0
    occurrence_ts: List[Optional[float]] = field(default_factory=list)
    occurrence_classes: List[str] = field(default_factory=list)
    extra_by_class: Dict[str, int] = field(default_factory=dict)
```

3c. Extend `TokenStats` (after its existing `repeat_extra_tokens: int = 0` field):

```python
    repeat_class_totals: Dict[str, int] = field(default_factory=dict)
```

3d. Add three module-level helpers (directly above the existing `_repeats`):

```python
def _tool_names(events: List[Event]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for e in events:
        if e.type != "ToolCall":
            continue
        fn = e.payload.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else None
        call_id = e.payload.get("id")
        if call_id is not None and name:
            names[str(call_id)] = str(name)
    return names


def _classify_occurrences(ts_list: List[Optional[float]],
                          tools: Dict[int, Optional[str]],
                          chars_each: int,
                          windows: List[dict]) -> List[str]:
    classes = ["first"]
    has_source = bool(windows)
    for i in range(1, len(ts_list)):
        prev_ts, cur_ts = ts_list[i - 1], ts_list[i]
        if prev_ts is None or cur_ts is None:
            classes.append("unclassified")
            continue
        if any(w.get("window_start") is not None
               and w.get("window_end") is not None
               and w["window_start"] < cur_ts and w["window_end"] > prev_ts
               for w in windows):
            classes.append("post_compaction")
            continue
        if (chars_each < REPEAT_POLL_MAX_CHARS
                and tools.get(i) is not None
                and tools.get(i) == tools.get(i - 1)
                and (cur_ts - prev_ts) <= REPEAT_POLL_GAP_S):
            classes.append("poll")
            continue
        classes.append("no_compaction" if has_source else "unclassified")
    return classes


def _extra_by_class(classes: List[str], tokens_each: int) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for cls in classes[1:]:
        counts[cls] = counts.get(cls, 0) + tokens_each
    return counts
```

3e. Replace the whole existing `_repeats` function (currently `def _repeats(events: List[Event]) -> Tuple[List[Repeat], int]:` ... `return repeats[:10], sum(r.extra_tokens for r in repeats)`) with:

```python
def _repeats(events: List[Event],
             compaction_windows: Optional[List[dict]] = None,
             ) -> Tuple[List[Repeat], int, Dict[str, int]]:
    """Identical large tool results seen more than once. Each occurrence
    after the first is classified against compaction windows:
    post_compaction (legal protocol re-read), poll, no_compaction (the real
    violation), or unclassified when no evidence source exists. Returns the
    top-10 repeats, the extra-token total over ALL groups, and per-class
    totals over ALL groups, so class totals add up against the headline."""
    windows = compaction_windows or []
    tools_by_id = _tool_names(events)
    groups: Dict[str, dict] = {}
    for e in events:
        if e.type != "ToolResult":
            continue
        out = _event_text(e)
        if len(out) < MIN_REPEAT_CHARS:
            continue
        key = hashlib.sha1(out.encode("utf-8", "replace")).hexdigest()
        g = groups.setdefault(
            key,
            {"preview": out[:160], "occurrences": 0,
             "tokens": estimate_tokens(out), "chars": len(out),
             "ts": [], "tools": []},
        )
        g["occurrences"] += 1
        g["ts"].append(e.ts)
        g["tools"].append(tools_by_id.get(str(e.payload.get("tool_call_id"))))
    repeats: List[Repeat] = []
    total = 0
    class_totals: Dict[str, int] = {}
    for g in groups.values():
        if g["occurrences"] <= 1:
            continue
        classes = _classify_occurrences(g["ts"], g["tools"], g["chars"],
                                        windows)
        per_class = _extra_by_class(classes, g["tokens"])
        for cls, extra in per_class.items():
            class_totals[cls] = class_totals.get(cls, 0) + extra
        extra = sum(per_class.values())
        total += extra
        repeats.append(Repeat(
            preview=g["preview"], occurrences=g["occurrences"],
            tokens_each=g["tokens"], extra_tokens=extra,
            tool_name=(g["tools"][0] if len(set(g["tools"])) == 1 else None),
            chars_each=g["chars"], occurrence_ts=list(g["ts"]),
            occurrence_classes=classes, extra_by_class=per_class))
    repeats.sort(key=lambda r: -r.extra_tokens)
    return repeats[:10], total, class_totals
```

Important: keep `estimate_tokens(out)` (imported from `.timeline`) and `out[:160]` for the preview exactly as the old code had them — the 23709-style headline numbers depend on it.

3f. In `build_token_stats`, update the call site (currently `stats.repeats, stats.repeat_extra_tokens = _repeats(session.events)` at ~line 463) to:

```python
    stats.repeats, stats.repeat_extra_tokens, stats.repeat_class_totals = \
        _repeats(session.events)
```

Without this exact change the entire suite breaks on tuple unpacking — it is part of this task, not a later one.

- [ ] **Step 4: Run the new tests, then the full suite**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py`
Expected: all pass.

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass — `build_token_stats`'s new `repeat_class_totals` attribute is additive; the report and web render paths read only fields that still exist.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/tokenstats.py tests/test_tokenstats.py
git commit -m "feat: classify repeat-injection occurrences in tokenstats"
```

---

### Task 4: `merge_compaction_windows` + `build_token_stats` params

**Files:**
- Modify: `src/agent_session_detective/tokenstats.py` (`TokenStats` gains two fields after `repeat_class_totals`; new `merge_compaction_windows` function; `build_token_stats` gains two keyword params)
- Test: `tests/test_tokenstats.py` (extend imports; append two test classes before `if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_tokenstats.py`, the imports become:

```python
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_session_detective.tokenstats import (
    _classify_occurrences,
    _extra_by_class,
    _repeats,
    build_token_stats,
    merge_compaction_windows,
)
from agent_session_detective.wire import Event
```

Append before the final `if __name__ == "__main__":` lines:

```python
class MergeCompactionWindowsTest(unittest.TestCase):
    def test_transcript_windows_come_from_timeline_compactions(self):
        timeline = SimpleNamespace(compactions=[
            SimpleNamespace(begin_ts=10.0, end_ts=25.0)])
        windows, source = merge_compaction_windows(timeline)
        self.assertEqual(source, "transcript")
        self.assertEqual(windows, [
            {"ts": 10.0, "window_start": 10.0, "window_end": 25.0}])

    def test_open_compaction_ends_at_its_begin(self):
        timeline = SimpleNamespace(compactions=[
            SimpleNamespace(begin_ts=10.0, end_ts=None)])
        windows, source = merge_compaction_windows(timeline)
        self.assertEqual(source, "transcript")
        self.assertEqual(windows, [
            {"ts": 10.0, "window_start": 10.0, "window_end": 10.0}])

    def test_compaction_without_begin_ts_is_skipped(self):
        timeline = SimpleNamespace(compactions=[
            SimpleNamespace(begin_ts=None, end_ts=None)])
        self.assertEqual(merge_compaction_windows(timeline), ([], None))

    def test_billed_points_are_appended_after_transcript(self):
        timeline = SimpleNamespace(compactions=[
            SimpleNamespace(begin_ts=10.0, end_ts=25.0)])
        billed = [{"ts": 110.0, "window_start": 100.0, "window_end": 110.0,
                   "pre_prompt": 100000, "post_prompt": 50000}]
        windows, source = merge_compaction_windows(timeline, billed)
        self.assertEqual(source, "transcript+billed")
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[1]["post_prompt"], 50000)

    def test_billed_only(self):
        billed = [{"ts": 110.0, "window_start": 100.0, "window_end": 110.0,
                   "pre_prompt": 100000, "post_prompt": 50000}]
        windows, source = merge_compaction_windows(
            SimpleNamespace(compactions=[]), billed)
        self.assertEqual(source, "billed")
        self.assertEqual(len(windows), 1)


class BuildTokenStatsTest(unittest.TestCase):
    def test_defaults_have_no_compaction_source(self):
        stats = build_token_stats(SimpleNamespace(events=[]),
                                  SimpleNamespace(compactions=[]))
        self.assertIsNone(stats.compaction_source)
        self.assertEqual(stats.compaction_points, [])
        self.assertEqual(stats.repeat_class_totals, {})

    def test_explicit_windows_are_stored(self):
        windows = [{"ts": 110.0, "window_start": 100.0, "window_end": 110.0,
                    "pre_prompt": 100000, "post_prompt": 50000}]
        stats = build_token_stats(
            SimpleNamespace(events=[]), SimpleNamespace(compactions=[]),
            compaction_windows=windows, compaction_source="billed")
        self.assertEqual(stats.compaction_source, "billed")
        self.assertEqual(stats.compaction_points, windows)
```

(These are safe with bare `SimpleNamespace` because `_turn_growth` returns `[]` before touching the timeline when the session has no context measurements — tokenstats.py:309.)

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py`
Expected: FAIL/ERROR with `ImportError: cannot import name 'merge_compaction_windows'`.

- [ ] **Step 3: Implement**

3a. In `TokenStats`, after `repeat_class_totals`, add:

```python
    compaction_source: Optional[str] = None
    compaction_points: List[dict] = field(default_factory=list)
```

3b. Add the merge function (directly above `build_token_stats`):

```python
def merge_compaction_windows(timeline: Timeline,
                             billed_points: Optional[List[dict]] = None,
                             ) -> Tuple[List[dict], Optional[str]]:
    """Compaction evidence windows from both sources. Transcript windows are
    [begin_ts, end_ts or begin_ts]; billed points already carry their
    [prev_ts, drop_ts] window. Source string names what backed the split."""
    windows: List[dict] = []
    sources: List[str] = []
    for c in timeline.compactions:
        if not c.begin_ts:
            continue
        windows.append({"ts": c.begin_ts, "window_start": c.begin_ts,
                        "window_end": c.end_ts or c.begin_ts})
    if windows:
        sources.append("transcript")
    for p in billed_points or []:
        windows.append(dict(p))
    if billed_points:
        sources.append("billed")
    return windows, "+".join(sources) or None
```

3c. Change the `build_token_stats` signature from
`def build_token_stats(session: Session, timeline: Timeline) -> TokenStats:` to:

```python
def build_token_stats(
    session: Session,
    timeline: Timeline,
    compaction_windows: Optional[List[dict]] = None,
    compaction_source: Optional[str] = None,
) -> TokenStats:
```

and replace the `_repeats` call site (from Task 3) with:

```python
    stats.repeats, stats.repeat_extra_tokens, stats.repeat_class_totals = \
        _repeats(session.events, compaction_windows)
    stats.compaction_source = compaction_source or None
    stats.compaction_points = list(compaction_windows or [])
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py`
Expected: all pass.

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass (both new params default to None; existing call sites unchanged).

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/tokenstats.py tests/test_tokenstats.py
git commit -m "feat: merge transcript and billed compaction windows"
```

---

### Task 5: CLI wiring — opt-in billed classification

**Files:**
- Modify: `src/agent_session_detective/cli.py` (billing import ~line 12; tokenstats import ~line 19; replace `token_stats = build_token_stats(session, timeline)` ~line 163)
- Test: `tests/test_cli.py` (append two methods inside `BilledUsageFlagTests`, after `test_without_the_flag_no_attach_happens` which ends ~line 290, before `if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli.py`, insert after the last line of `test_without_the_flag_no_attach_happens` (`for note in document.coverage.notes))`) — keeping the same 4-space method indentation:

```python
    def test_billed_usage_feeds_compaction_windows_into_token_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            db = base / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_message "
                "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
            conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
                BILLING_UUID, 1754709632658,
                json.dumps({"prompt_tokens": 100000, "completion_tokens": 40,
                            "cached_tokens": 80000})))
            conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
                BILLING_UUID, 1754709642658,
                json.dumps({"prompt_tokens": 50000, "completion_tokens": 40,
                            "cached_tokens": 0})))
            conn.commit()
            conn.close()

            with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                       str(db)), patch(
                           "agent_session_detective.cli.render_report",
                           return_value="<html>report</html>") as render:
                exit_code = cli.main([
                    str(transcript), "--no-judge", "--billed-usage",
                    "--billed-db", str(db)])

            self.assertEqual(exit_code, 0)
            stats = render.call_args.kwargs["token_stats"]
            self.assertEqual(stats.compaction_source, "billed")
            self.assertEqual(len(stats.compaction_points), 1)
            self.assertEqual(stats.compaction_points[0]["post_prompt"], 50000)

    def test_without_billed_usage_classification_has_no_source(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            stats = render.call_args.kwargs["token_stats"]
            self.assertIsNone(stats.compaction_source)
            self.assertEqual(stats.compaction_points, [])
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cli.py -k billed`
Expected: the new billed test FAILS (`AttributeError: 'TokenStats' object has no attribute 'compaction_source'`); the new without-billed test PASSES (source is already None); pre-existing billed tests pass.

- [ ] **Step 3: Implement in cli.py**

3a. The billing import becomes:

```python
from .billing import (
    attach_billed_usage,
    billed_compaction_points,
    session_uuid_from_source,
)
```

3b. The tokenstats import becomes:

```python
from .tokenstats import build_token_stats, merge_compaction_windows
```

3c. Replace the single line `token_stats = build_token_stats(session, timeline)` with:

```python
    billed_points = []
    if args.billed_usage:
        billed_uuid = session_uuid_from_source(session_source)
        if billed_uuid is not None:
            billed_points, billed_error = billed_compaction_points(
                billed_uuid, db_path=args.billed_db)
            if billed_error:
                document.coverage.notes.append(
                    "billed_compaction: unavailable (%s)" % billed_error)
    compaction_windows, compaction_source = merge_compaction_windows(
        timeline, billed_points)
    token_stats = build_token_stats(
        session, timeline,
        compaction_windows=compaction_windows, compaction_source=compaction_source)
```

(`document` is already built ~line 127, before this point; `session_source` is the `Path` the CLI already uses for `attach_billed_usage`.)

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_cli.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/cli.py tests/test_cli.py
git commit -m "feat: wire compaction classification into the CLI"
```

---

### Task 6: Web serialization + cache key + fingerprint v9 (tests FIRST)

**Files:**
- Modify: `tests/test_web.py` (rename the v8 fingerprint test ~line 58; add a v9 invalidation test beside it; add a tokenstats import after the existing `from agent_session_detective.ir.schema import IR_VERSION` line; append two test classes before `if __name__ == "__main__":`)
- Modify: `src/agent_session_detective/web.py` (`cache_key` ~lines 39-49; `fingerprint` comment chain + return ~lines 78-93; `tokenstats_to_dict` repeats block ~lines 214-229)

- [ ] **Step 1: Write the failing tests (including the v8→v9 rename)**

In `tests/test_web.py`:

1a. Add a new import after the existing `from agent_session_detective.ir.schema import IR_VERSION` line:

```python
from agent_session_detective.tokenstats import Repeat, TokenStats
```

1b. Replace the existing v8 fingerprint test (exact current text):

```python
    def test_fingerprint_uses_v8_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v8:ir%s" % IR_VERSION,
            )
```

with:

```python
    def test_fingerprint_uses_v9_and_ir_version_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(
                fingerprint(str(transcript), "test-model"),
                "1234.000:5:test-model:v9:ir%s" % IR_VERSION,
            )

    def test_v9_fingerprint_invalidates_v8_cached_results(self):
        # v8 cached results carry repeats without per-occurrence classes;
        # the v9 bump must make cache_load miss.
        v8_fp = "1234.000:5:test-model:v8:ir1.6"
        v9_fp = "1234.000:5:test-model:v9:ir1.6"
        result = {"status_line": "stale v8 payload"}

        original_cache_dir = web.CACHE_DIR
        web.CACHE_DIR = Path(tempfile.mkdtemp())
        try:
            cache_store("k", v8_fp, result)
            self.assertEqual(cache_load("k", v8_fp), result)
            self.assertIsNone(cache_load("k", v9_fp))
        finally:
            web.CACHE_DIR = original_cache_dir
```

(The existing `test_an_ir_version_bump_invalidates_cached_results` stays untouched — it keeps pinning the IR self-invalidation semantics.)

1c. Append before the final `if __name__ == "__main__":` lines:

```python
class TokenstatsSerializationTests(unittest.TestCase):
    def _stats(self):
        repeat = Repeat(
            preview="same output", occurrences=3, tokens_each=100,
            extra_tokens=200, tool_name="Read", chars_each=300,
            occurrence_classes=["first", "post_compaction", "poll"],
            extra_by_class={"post_compaction": 100, "poll": 100})
        return TokenStats(
            repeats=[repeat], repeat_extra_tokens=200,
            repeat_class_totals={"post_compaction": 100, "poll": 100},
            compaction_source="transcript+billed",
            compaction_points=[{"ts": 110.0, "window_start": 100.0,
                                "window_end": 110.0, "pre_prompt": 100000,
                                "post_prompt": 50000}])

    def test_repeats_carry_classification_fields(self):
        payload = web.tokenstats_to_dict(self._stats())
        entry = payload["repeats"][0]
        self.assertEqual(entry["occurrence_classes"],
                         ["first", "post_compaction", "poll"])
        self.assertEqual(entry["extra_by_class"],
                         {"post_compaction": 100, "poll": 100})
        self.assertEqual(entry["tool_name"], "Read")
        self.assertEqual(entry["chars_each"], 300)
        # raw occurrence timestamps stay internal
        self.assertNotIn("occurrence_ts", entry)

    def test_top_level_classification_fields(self):
        payload = web.tokenstats_to_dict(self._stats())
        self.assertEqual(payload["repeat_class_totals"],
                         {"post_compaction": 100, "poll": 100})
        self.assertEqual(payload["compaction_source"], "transcript+billed")
        self.assertEqual(payload["compaction_points"][0]["post_prompt"], 50000)


class CacheKeyTests(unittest.TestCase):
    def test_billed_flag_changes_the_cache_key(self):
        base = {"path": "/tmp/x.jsonl", "expect": None, "steps": None,
                "judge_triggers": False}
        self.assertNotEqual(web.cache_key(base),
                            web.cache_key({**base, "billed": True}))
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_web.py`
Expected: FAIL — `test_fingerprint_uses_v9_...` (fingerprint still returns v8), both `TokenstatsSerializationTests` tests (fields missing → `KeyError`), `CacheKeyTests` (keys equal). The new invalidation test passes (it pins miss semantics with explicit fingerprints), as does the untouched IR-bump test. All other tests pass.

- [ ] **Step 3: Implement in web.py**

3a. In `cache_key`, add one dict entry after `"judge_triggers": bool(params.get("judge_triggers")),`:

```python
            "billed": bool(params.get("billed")),
```

3b. In `fingerprint`, append one line to the version comment chain (after the `# v8 appends ir<IR_VERSION>...` lines) and bump the counter, keeping the `:ir%s` suffix:

```python
    # v9 adds repeat-injection classification (per-occurrence classes,
    # repeat_class_totals, compaction_source/points) — v8 cached results
    # would render repeats without the class badges and the split caption.
    return "%.3f:%d:%s:v9:ir%s" % (newest, total, judge_model, IR_VERSION)
```

3c. In `tokenstats_to_dict`, extend each repeats entry:

```python
        "repeats": [
            {
                "preview": r.preview,
                "occurrences": r.occurrences,
                "tokens_each": r.tokens_each,
                "extra_tokens": r.extra_tokens,
                "tool_name": r.tool_name,
                "chars_each": r.chars_each,
                "occurrence_classes": r.occurrence_classes,
                "extra_by_class": r.extra_by_class,
            }
            for r in s.repeats
        ],
        "repeat_extra_tokens": s.repeat_extra_tokens,
        "repeat_class_totals": s.repeat_class_totals,
        "compaction_source": s.compaction_source,
        "compaction_points": s.compaction_points,
        "usage_record_count": len(s.usage_records),
```

(`occurrence_ts` is deliberately NOT serialized.)

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_web.py`
Expected: all pass.

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: serialize repeat classification to the web payload (v9)"
```

---

### Task 7: run_audit billed classification, fleet rollup, suggestion split

**Files:**
- Modify: `src/agent_session_detective/web.py` (tokenstats import ~line 23; `fleet_stats` ~lines 407-446; `suggest_next_steps` repeat block ~lines 512-516; `run_audit` ~lines 595-596)
- Test: `tests/test_web.py` (add imports; append three test classes before `if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_web.py`, add two imports (with the other stdlib/third-party imports at the top):

```python
from types import SimpleNamespace
from unittest.mock import patch
```

Append before the final `if __name__ == "__main__":` lines:

```python
class RunAuditBilledTests(unittest.TestCase):
    BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"

    def _sawtooth_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE chat_message "
            "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
            self.BILLING_UUID, 1754709632658,
            json.dumps({"prompt_tokens": 100000, "completion_tokens": 40,
                        "cached_tokens": 80000})))
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)", (
            self.BILLING_UUID, 1754709642658,
            json.dumps({"prompt_tokens": 50000, "completion_tokens": 40,
                        "cached_tokens": 0})))
        conn.commit()
        conn.close()

    def _transcript(self, directory):
        transcript = Path(directory) / (self.BILLING_UUID + ".jsonl")
        transcript.write_text(
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.call", "toolCallId": "c1", "name": "Read", '
            '"args": {"path": "a"}}}\n'
            '{"type": "context.append_loop_event", "event": '
            '{"type": "tool.result", "toolCallId": "c1", "result": '
            '{"output": "hello world"}}}\n',
            encoding="utf-8")
        return transcript

    def test_billed_audit_classifies_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "local.db"
            self._sawtooth_db(db)
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                           str(db)):
                    job = web.Job({"path": str(transcript), "billed": True})
                    web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                self.assertEqual(stats["compaction_source"], "billed")
                self.assertEqual(len(stats["compaction_points"]), 1)
                self.assertEqual(stats["compaction_points"][0]["post_prompt"],
                                 50000)
            finally:
                web.CACHE_DIR = original_cache_dir

    def test_audit_without_billed_has_no_compaction_source(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = self._transcript(directory)
            original_cache_dir = web.CACHE_DIR
            web.CACHE_DIR = Path(tempfile.mkdtemp())
            try:
                job = web.Job({"path": str(transcript)})
                web.run_audit(job)
                self.assertEqual(job.status, "done")
                stats = job.result["token_stats"]
                self.assertIsNone(stats["compaction_source"])
            finally:
                web.CACHE_DIR = original_cache_dir


class FleetClassTotalsTests(unittest.TestCase):
    BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"

    def test_fleet_payload_carries_repeat_class_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "ws"
            workspace.mkdir()
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl",
                        workspace / (self.BILLING_UUID + ".jsonl"))
            fleet = web.fleet_stats([str(base)])
            self.assertIn("repeat_class_totals", fleet)


class SuggestNextStepsRepeatTests(unittest.TestCase):
    def _timeline(self):
        return SimpleNamespace(file_reads=[], loads=[])

    def test_with_source_splits_legal_rereads_from_tax(self):
        token_stats = TokenStats(
            repeat_extra_tokens=23000,
            repeat_class_totals={"post_compaction": 22900, "poll": 100},
            compaction_source="billed")
        out = web.suggest_next_steps(self._timeline(), [], [], [], False,
                                     token_stats)
        self.assertTrue(any("合法重读" in line for line in out))
        self.assertTrue(any("真实重读税仅 100" in line for line in out))

    def test_without_source_keeps_the_externalize_message(self):
        token_stats = TokenStats(
            repeat_extra_tokens=23000,
            repeat_class_totals={"unclassified": 23000})
        out = web.suggest_next_steps(self._timeline(), [], [], [], False,
                                     token_stats)
        self.assertTrue(any("状态外置" in line for line in out))
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_web.py`
Expected: FAIL — `test_billed_audit_classifies_repeats` (source is None, billed param ignored), `test_fleet_payload_carries_repeat_class_totals` (KeyError), `test_with_source_splits_legal_rereads_from_tax` (old single message). The without-billed and without-source tests pass already. All other tests pass.

- [ ] **Step 3: Implement in web.py**

3a. The tokenstats import (~line 23) becomes:

```python
from .tokenstats import (
    BUCKET_KEYS,
    TokenStats,
    build_token_stats,
    merge_compaction_windows,
)
```

3b. In `fleet_stats`, extend the init dict after `"total_repeat_extra_tokens": 0,`:

```python
        "repeat_class_totals": {},
```

3c. In the `try` block, replace `stats = build_token_stats(session, timeline)` with:

```python
            windows, source = merge_compaction_windows(timeline)
            stats = build_token_stats(
                session, timeline,
                compaction_windows=windows, compaction_source=source)
```

3d. In the `except` block, after `s["repeat_extra_tokens"] = s["output_total"] = None` add:

```python
            s["repeat_class_totals"] = None
```

3e. After the accumulation line `fleet["total_repeat_extra_tokens"] += s["repeat_extra_tokens"] or 0`, add:

```python
        for cls, extra in (s["repeat_class_totals"] or {}).items():
            fleet["repeat_class_totals"][cls] = \
                fleet["repeat_class_totals"].get(cls, 0) + extra
```

3f. In `suggest_next_steps`, replace the repeat block:

```python
        if token_stats.repeat_extra_tokens >= 5000:
            out.append(
                "重复注入约 %d token: 同一份工具结果被多次读入，考虑状态外置 + 派生摘要"
                % token_stats.repeat_extra_tokens
            )
```

with:

```python
        if token_stats.repeat_extra_tokens >= 5000:
            post = (token_stats.repeat_class_totals or {}).get(
                "post_compaction", 0)
            tax = token_stats.repeat_extra_tokens - post
            if tax < 5000 and token_stats.compaction_source:
                out.append(
                    "重复注入 %d token 属压缩恢复协议内合法重读: 真实重读税仅 %d"
                    % (token_stats.repeat_extra_tokens, tax)
                )
            else:
                out.append(
                    "重复注入约 %d token: 同一份工具结果被多次读入，考虑状态外置 + 派生摘要"
                    % token_stats.repeat_extra_tokens
                )
```

The gate stays ≥5000 on the TOTAL (unchanged behavior when no source exists).

3g. In `run_audit`, replace the two lines:

```python
        job.mark("deriving next steps")
        token_stats = build_token_stats(session, timeline)
```

with:

```python
        job.mark("deriving next steps")
        billed_points = []
        if job.params.get("billed"):
            from .billing import billed_compaction_points, session_uuid_from_source
            billed_uuid = session_uuid_from_source(source_path)
            if billed_uuid is not None:
                billed_points, billed_error = billed_compaction_points(billed_uuid)
                if billed_error:
                    job.mark("billed_compaction unavailable: %s" % billed_error)
        compaction_windows, compaction_source = merge_compaction_windows(timeline, billed_points)
        token_stats = build_token_stats(
            session, timeline,
            compaction_windows=compaction_windows, compaction_source=compaction_source)
```

(The IR document is built later in `run_audit`, so an unavailable side channel goes to `job.mark(...)`, not `document.coverage.notes`.)

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_web.py`
Expected: all pass.

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/web.py tests/test_web.py
git commit -m "feat: classify repeats in web audits, fleet rollup and suggestions"
```

---

### Task 8: HTML report surfaces

**Files:**
- Modify: `src/agent_session_detective/report.py` (module constant before `_render_token_governance` ~line 161; repeats block inside it ~lines 249-262)
- Test: `tests/test_tokenstats.py` (extend imports; append a test class before `if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_tokenstats.py`, the tokenstats/report imports become:

```python
from agent_session_detective.report import _render_token_governance
from agent_session_detective.tokenstats import (
    Repeat,
    TokenStats,
    _classify_occurrences,
    _extra_by_class,
    _repeats,
    build_token_stats,
    merge_compaction_windows,
)
```

Append before the final `if __name__ == "__main__":` lines:

```python
class RenderTokenGovernanceRepeatsTest(unittest.TestCase):
    def _repeat(self, classes):
        counts = {}
        for cls in classes[1:]:
            counts[cls] = counts.get(cls, 0) + 100
        return Repeat(preview="same output", occurrences=3, tokens_each=100,
                      extra_tokens=200, tool_name="Read", chars_each=300,
                      occurrence_classes=classes, extra_by_class=counts)

    def _stats(self, source):
        if source:
            classes = ["first", "post_compaction", "post_compaction"]
            class_totals = {"post_compaction": 200}
            points = [{"window_start": 5.0, "window_end": 50.0}]
        else:
            classes = ["first", "unclassified", "unclassified"]
            class_totals = {"unclassified": 200}
            points = []
        repeat = self._repeat(classes)
        return TokenStats(repeats=[repeat], repeat_extra_tokens=200,
                          repeat_class_totals=class_totals,
                          compaction_source=source,
                          compaction_points=points)

    def test_with_source_captions_legal_rereads(self):
        html = _render_token_governance(self._stats("transcript+billed"))
        self.assertIn("压缩恢复 ×2", html)
        self.assertIn("协议内合法重读", html)
        self.assertIn("重读税下界", html)
        self.assertNotIn("状态未外置", html)

    def test_without_source_stays_neutral(self):
        html = _render_token_governance(self._stats(None))
        self.assertIn("未分类 ×2", html)
        self.assertIn("无压缩证据源", html)
        self.assertNotIn("状态未外置", html)
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py -k RenderTokenGovernance`
Expected: both FAIL — no `压缩恢复 ×2` / `未分类 ×2` in the current 4-column table.

- [ ] **Step 3: Implement in report.py**

3a. Immediately ABOVE `def _render_token_governance(stats: TokenStats) -> str:` add the module constant:

```python
REPEAT_CLASS_LABELS = {
    "post_compaction": "压缩恢复",
    "poll": "轮询",
    "no_compaction": "无压缩",
    "unclassified": "未分类",
}
```

3b. Replace the repeats block inside `_render_token_governance` (from `if stats.repeats:` through the `"...replace the replay.</p>" "% 200)` append, keeping the final `return "\n".join(out)`) with:

```python
    if stats.repeats:
        out.append("<details><summary>repeat injection (%d groups, ~%d extra tokens)"
                   "</summary><div class='body'><table>"
                   "<tr><th>occurrences</th><th>~tokens each</th><th>extra tokens</th>"
                   "<th>classes</th><th>content</th></tr>"
                   % (len(stats.repeats), stats.repeat_extra_tokens))
        for r in stats.repeats:
            counts: dict = {}
            for cls in r.occurrence_classes[1:]:
                counts[cls] = counts.get(cls, 0) + 1
            classes = " · ".join(
                "%s ×%d" % (REPEAT_CLASS_LABELS.get(c, c), n)
                for c, n in sorted(counts.items())) or "-"
            out.append(
                "<tr><td>%d</td><td>%d</td><td>%d</td><td>%s</td><td>%s</td></tr>"
                % (r.occurrences, r.tokens_each, r.extra_tokens, esc(classes),
                   esc(r.preview[:120]))
            )
        post = stats.repeat_class_totals.get("post_compaction", 0)
        tax = stats.repeat_extra_tokens - post
        if stats.compaction_source:
            out.append(
                "</table><p class='meta'>压缩点 %d 个（来源 %s）：其中压缩恢复 ~%d token "
                "属压缩恢复协议内合法重读，其余 ~%d 才是重读税。被编辑文件（如 progress.md）"
                "内容每次不同，不构成重复组——extra 总量是重读税下界。</p></div></details>"
                % (len(stats.compaction_points), stats.compaction_source, post, tax))
        else:
            out.append(
                "</table><p class='meta'>无压缩证据源，未分类；--billed-usage 可接入账单证据"
                "（账单锯齿可识别 CLI 形会话的压缩点）。</p></div></details>")
    return "\n".join(out)
```

Note: `counts: dict = {}` (plain `dict`, not `Dict`) — report.py does not import `Dict`.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python3 -m pytest -q tests/test_tokenstats.py`
Expected: all pass.

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git status
git add src/agent_session_detective/report.py tests/test_tokenstats.py
git commit -m "feat: render repeat classification in the report"
```

---

### Task 9: Webapp UI surfaces + live verification

**Files:**
- Modify: `src/agent_session_detective/webapp/app.js` (fleet badge ~lines 205-206; token governance repeats block ~lines 544-553)

No JS unit tests exist in this repo; verification is live (below). Do not commit until the live checks pass.

- [ ] **Step 1: Fleet badge split**

Replace (app.js ~205-206):

```js
      badge(fleet.total_repeat_extra_tokens >= 5000 ? "warn" : "dim",
            "repeat +" + fleet.total_repeat_extra_tokens);
```

with:

```js
      badge(fleet.total_repeat_extra_tokens >= 5000 ? "warn" : "dim",
            "repeat +" + fleet.total_repeat_extra_tokens +
            (fleet.total_repeat_extra_tokens
              ? " (压缩恢复 " + (((fleet.repeat_class_totals || {}).post_compaction) || 0) +
                " / 其它 " + Math.max(0, fleet.total_repeat_extra_tokens -
                  (((fleet.repeat_class_totals || {}).post_compaction) || 0)) + ")"
              : ""));
```

- [ ] **Step 2: Repeats table + split caption**

Replace the repeats block (app.js ~544-553):

```js
    if (t.repeats && t.repeats.length) {
      var rrows = t.repeats.map(function (r) {
        return "<tr><td>" + r.occurrences + "×</td><td>~" + r.tokens_each + "</td><td>~" +
          r.extra_tokens + '</td><td class="dim">' + esc(r.preview.slice(0, 100)) + "</td></tr>";
      }).join("");
      parts.push("<details><summary>" + badge("warn", "repeat injection") + " " + t.repeats.length +
        " 组 · ~" + t.repeat_extra_tokens + " extra tokens</summary>" +
        "<table><tr><th>次数</th><th>每次~tokens</th><th>多付</th><th>内容</th></tr>" + rrows + "</table>" +
        '<p class="dim">同一份工具结果（≥200 字符）被多次读入 = 状态未外置的可见信号。</p></details>');
    }
```

with:

```js
    if (t.repeats && t.repeats.length) {
      var classLabels = {post_compaction: "压缩恢复", poll: "轮询",
                         no_compaction: "无压缩", unclassified: "未分类"};
      var rrows = t.repeats.map(function (r) {
        var counts = {};
        (r.occurrence_classes || []).slice(1).forEach(function (c) {
          counts[c] = (counts[c] || 0) + 1;
        });
        var classes = Object.keys(counts).sort().map(function (c) {
          return (classLabels[c] || c) + " ×" + counts[c];
        }).join(" · ") || "-";
        return "<tr><td>" + r.occurrences + "×</td><td>~" + r.tokens_each + "</td><td>~" +
          r.extra_tokens + '</td><td class="dim">' + esc(classes) + '</td><td class="dim">' +
          esc(r.preview.slice(0, 100)) + "</td></tr>";
      }).join("");
      var post = ((t.repeat_class_totals || {}).post_compaction) || 0;
      var tax = Math.max(0, t.repeat_extra_tokens - post);
      var caption = t.compaction_source
        ? '<p class="dim">压缩点 ' + (t.compaction_points || []).length +
          ' 个（来源 ' + esc(t.compaction_source) + '）：其中压缩恢复 ~' + post +
          ' token 属压缩恢复协议内合法重读，其余 ~' + tax +
          ' 才是重读税。extra 总量是重读税下界。</p>'
        : '<p class="dim">无压缩证据源，未分类；--billed-usage（CLI）/ billed=1（web）可接入账单证据。</p>';
      parts.push("<details><summary>" + badge("warn", "repeat injection") + " " + t.repeats.length +
        " 组 · ~" + t.repeat_extra_tokens + " extra tokens</summary>" +
        "<table><tr><th>次数</th><th>每次~tokens</th><th>多付</th><th>分类</th><th>内容</th></tr>" +
        rrows + "</table>" + caption + "</details>");
    }
```

- [ ] **Step 3: Live verification against the real b1d65022 session**

Run from repo root:

```bash
find ~/.qoder/projects -maxdepth 3 -name 'b1d65022*.jsonl' -not -path '*/subagents/*'
PYTHONPATH=src python3 -m agent_session_detective.cli --serve --port 8471 &
SERVER_PID=$!
```

Then (fill in the discovered transcript path):

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8471/api/audit \
  -H 'Content-Type: application/json' \
  -d '{"path": "<discovered-transcript-path>", "billed": true}')
echo "$JOB"
```

Poll `curl -s http://127.0.0.1:8471/api/job/<id-from-response>` until `"status": "done"`, then assert:

```bash
curl -s http://127.0.0.1:8471/api/job/<id> | python3 -c "
import sys, json
r = json.load(sys.stdin)['result']
ts = r['token_stats']
print('source:', ts['compaction_source'])
print('points:', len(ts['compaction_points']))
print('class_totals:', ts['repeat_class_totals'])
print('first repeat classes:', ts['repeats'][0]['occurrence_classes'])
"
```

Expected: `source` contains `billed` (b1d65022 is CLI-shape and may have no transcript compaction records — `billed` alone is the expected outcome; `transcript+billed` also acceptable), `points` >= 3 (three billed sawtooth drops are the known ground truth), `class_totals` carries `post_compaction` ≈ 23000+ of the ~23709 total, and repeat rows carry `occurrence_classes`.

Then verify the UI with the browser-use MCP tools: navigate to `http://127.0.0.1:8471`, open the b1d65022 session audit, and confirm (a) the fleet badge shows `repeat +N (压缩恢复 X / 其它 Y)`, (b) the repeat injection panel has a 分类 column with `压缩恢复 ×N` chips, (c) the caption is the split version (压缩点 … 合法重读 … 重读税), not the old 状态未外置 line.

Finally stop the server: `kill $SERVER_PID`.

- [ ] **Step 4: Commit**

```bash
git status
git add src/agent_session_detective/webapp/app.js
git commit -m "feat: render repeat classification in the tree webapp"
```

---

### Task 10: Full-suite verification + workspace sweep

**Files:** none modified (unless a fix is needed).

- [ ] **Step 1: Run the entire suite**

Run: `PYTHONPATH=src python3 -m pytest -q tests/`
Expected: all pass (baseline was 254 tests before this plan; the plan adds ~35).

- [ ] **Step 2: Workspace sweep (coding constitution 收尾扫描)**

Run: `git status` and `ls` the repo root. Remove any stray files created during this plan (none are expected — Task 9 wrote nothing to disk). Confirm `.changes/` and `tests/test_ir_intervention.py` are untouched: `git status --porcelain` must list nothing owned by this plan left uncommitted.

- [ ] **Step 3: Stop — no commit**

No commit unless a fix was required; if a fix was needed, commit only the fixed files by explicit path.

---

## Self-review record (completed at plan-writing time)

- **Spec coverage:** dual-source windows (Tasks 1/2/4/5/7), classification core + priority chain (Task 3), no-source neutral handling (Tasks 3/8/9), honest unavailability notes (Tasks 2/5/7), v9 cache invalidation (Task 6), all three surfaces + fleet badge + suggestions (Tasks 6/7/8/9), live ground-truth verification (Task 9). No spec requirement lacks a task.
- **Type consistency:** `_repeats` 3-tuple defined in Task 3 and consumed at the Task 3 call site and Task 4 params; `merge_compaction_windows` defined Task 4, called Tasks 5/7; `billed_compaction_points` defined Task 2, called Tasks 5/7; window dict keys `{ts, window_start, window_end[, pre_prompt, post_prompt]}` consistent across Tasks 2/4/6; `Repeat`/`TokenStats` new field names identical across Tasks 3/4/6/8 and the JS reads in Task 9.
- **Deliberate refinements vs spec** (documented in Design notes): transcript windows `[begin_ts, end_ts or begin_ts]` instead of point windows; `_repeats` 3-tuple so class totals add up against the headline total.
