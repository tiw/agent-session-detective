"""Repeat-injection classification: per-occurrence classes and totals."""

import unittest
from pathlib import Path
from types import SimpleNamespace

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
