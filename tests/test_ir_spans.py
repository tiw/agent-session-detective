# tests/test_ir_spans.py
import unittest

from agent_session_detective.ir.records import group_records
from agent_session_detective.ir.spans import (
    anchor_runs,
    positional_spans,
    verify_against_anchors,
)
from tests.ir_helpers import IREventsTestCase


def assistant(seq, text="x", anchor=None, usage=None):
    record = {
        "type": "assistant",
        "uuid": "a%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if anchor is not None:
        record["requestTokenAnchor"] = anchor
    if usage is not None:
        record["message"]["usage"] = usage
    return record


def user(seq, text="go"):
    return {
        "type": "user",
        "uuid": "u%d" % seq,
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def meta(seq):
    return {
        "type": "runtime-config",
        "timestamp": "2026-10-07T08:00:%02d.000Z" % seq,
        "model": "m",
        "contextWindow": 128000,
    }


def anchor(key):
    return {"request": key * 8, "response": key * 8, "requestId": "req-%s" % key}


class SpansTest(IREventsTestCase):
    def records(self, lines):
        return group_records(self.load_lines(lines).events)

    def test_span_closes_on_positive_usage(self):
        records = self.records(
            [
                user(1),
                assistant(2, text="working"),
                assistant(3, text="", usage={"input_tokens": 7, "output_tokens": 1}),
            ]
        )
        spans, ungroupable = positional_spans(records)

        self.assertEqual(ungroupable, [])
        self.assertEqual(len(spans), 1)
        self.assertEqual([record.seq for record in spans[0].records], [2, 3])
        self.assertEqual(spans[0].first_seq, 2)
        self.assertEqual(spans[0].last_seq, 3)
        self.assertEqual(spans[0].n_records, 2)
        self.assertEqual(spans[0].usage_record.seq, 3)

    def test_zero_usage_does_not_close_span(self):
        records = self.records(
            [
                user(1),
                assistant(2, usage={"input_tokens": 0, "output_tokens": 0,
                                    "cache_read_input_tokens": 0,
                                    "cache_creation_input_tokens": 0}),
                assistant(3, usage={"input_tokens": 7, "output_tokens": 1}),
            ]
        )
        spans, ungroupable = positional_spans(records)

        self.assertEqual(ungroupable, [])
        self.assertEqual(len(spans), 1)
        self.assertEqual([record.seq for record in spans[0].records], [2, 3])

    def test_user_record_flushes_open_run_as_ungroupable(self):
        records = self.records([assistant(1, text="dangling"), user(2)])
        spans, ungroupable = positional_spans(records)

        self.assertEqual(spans, [])
        self.assertEqual([record.seq for record in ungroupable], [1])

    def test_eof_flushes_open_run_as_ungroupable(self):
        records = self.records([assistant(1, text="dangling")])
        spans, ungroupable = positional_spans(records)

        self.assertEqual(spans, [])
        self.assertEqual([record.seq for record in ungroupable], [1])

    def test_meta_records_do_not_break_or_join_anchor_runs(self):
        records = self.records(
            [
                assistant(1, anchor=anchor("a")),
                meta(2),
                assistant(3, anchor=anchor("a")),
            ]
        )
        runs = anchor_runs(records)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].request_id, "req-a")
        self.assertEqual([record.seq for record in runs[0].records], [1, 3])

    def test_user_record_breaks_anchor_runs(self):
        records = self.records(
            [
                assistant(1, anchor=anchor("a")),
                user(2),
                assistant(3, anchor=anchor("a")),
            ]
        )
        runs = anchor_runs(records)

        self.assertEqual(len(runs), 2)
        self.assertEqual([record.seq for record in runs[0].records], [1])
        self.assertEqual([record.seq for record in runs[1].records], [3])

    def test_verification_agrees_when_run_and_span_match(self):
        records = self.records(
            [
                user(1),
                assistant(2, anchor=anchor("a")),
                assistant(3, anchor=anchor("a"),
                          usage={"input_tokens": 7, "output_tokens": 1}),
            ]
        )
        spans, _ = positional_spans(records)
        runs = anchor_runs(records)

        checked, disagreements = verify_against_anchors(spans, runs)

        self.assertEqual(checked, 1)
        self.assertEqual(disagreements, [])

    def test_verification_reports_mismatch(self):
        records = self.records(
            [
                user(1),
                assistant(2, anchor=anchor("a")),
                assistant(3, anchor=anchor("b"),
                          usage={"input_tokens": 7, "output_tokens": 1}),
            ]
        )
        spans, _ = positional_spans(records)
        runs = anchor_runs(records)

        checked, disagreements = verify_against_anchors(spans, runs)

        self.assertEqual(checked, 1)
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(disagreements[0]["run_seqs"], [2])
        self.assertEqual(disagreements[0]["span_seqs"], [2, 3])


if __name__ == "__main__":
    unittest.main()
