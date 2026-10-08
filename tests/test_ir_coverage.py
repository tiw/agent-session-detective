import unittest

from agent_session_detective.ir.coverage import build_coverage
from agent_session_detective.ir.items import Extraction
from agent_session_detective.ir.schema import (
    ContentItem,
    LLMCall,
    RequestInput,
    RequestOutput,
    Span,
)


def item(item_id, kind, channel, tokens=5):
    return ContentItem(
        item_id=item_id,
        agent_id="main",
        bucket="unattributed",
        kind=kind,
        name=None,
        channel=channel,
        wire_seq=1,
        gone_seq=None,
        size_chars=10,
        tokens_est=tokens,
        sha1="a" * 64,
        norm_sha1="a" * 64,
        record={"file": "f.jsonl", "seq": 1, "uuid": "u1"},
        preview="x",
    )


def call(call_id, agent_id="main", first_seq=2, last_seq=3, tier=1, **fields):
    values = dict(
        item_refs=[], buckets={}, anchor_tokens=None, unattributed_tokens=None,
        cache_read=0, cache_write_5m=0, cache_write_1h=0, context_usage_ratio=None,
        request_id=None, request_hash=None, response_hash=None, credits=None,
        prefix_hashes=None,
    )
    values.update(fields)
    return LLMCall(
        call_id=call_id,
        agent_id=agent_id,
        ts=100.0,
        model="m",
        span=Span(first_seq=first_seq, last_seq=last_seq, n_records=1),
        identity_tier=tier,
        input=RequestInput(**values),
        output=RequestOutput(parts=[], output_tokens=0),
    )


def coverage(calls, items, tiers=None, checked_calls=0, disagreements=None,
             ungroupable=None, extraction=None, dropped=None, notes=None):
    return build_coverage(
        calls,
        items,
        tiers=tiers if tiers is not None else {},
        checked_calls=checked_calls,
        disagreements=disagreements if disagreements is not None else [],
        ungroupable_records=ungroupable if ungroupable is not None else [],
        extraction=extraction if extraction is not None else Extraction(),
        dropped_records=dropped if dropped is not None else {},
        notes=notes if notes is not None else [],
    )


class CoverageTest(unittest.TestCase):
    def test_per_field_counts_fact_and_missing(self):
        identity = {"request_id": "req", "request_hash": "h",
                    "response_hash": "h2", "anchor_tokens": 1000}
        calls = [
            call("main:0", **dict(identity, context_usage_ratio=0.5,
                                  credits={"a": 1})),
            call("main:1", **identity),
            call("main:2", **identity),
        ]
        report = coverage(
            calls,
            [item("main:2:0", "assistant_text", "qoder:assistant"),
             item("main:3:0", "tool_result", "qoder:tool_result")],
            tiers={"main": 1},
            checked_calls=3,
        )

        self.assertEqual(report.per_field["anchor_tokens"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(report.per_field["request_id"],
                         {"fact": 3, "est": 0, "missing": 0})
        self.assertEqual(report.per_field["context_usage_ratio"],
                         {"fact": 1, "est": 0, "missing": 2})
        self.assertEqual(report.per_field["credits"],
                         {"fact": 1, "est": 0, "missing": 2})
        self.assertEqual(report.per_field["tokens_est"],
                         {"fact": 0, "est": 2, "missing": 0})
        self.assertEqual(report.requests_with_anchor, "3/3")

    def test_falsy_measured_values_still_count_as_fact(self):
        calls = [
            call("main:0", anchor_tokens=0, context_usage_ratio=0.0,
                 credits={}, request_hash=""),
            call("main:1"),
        ]
        report = coverage(calls, [], tiers={"main": 1})

        for field in ("anchor_tokens", "context_usage_ratio", "credits",
                      "request_hash"):
            self.assertEqual(report.per_field[field],
                             {"fact": 1, "est": 0, "missing": 1}, field)
        self.assertEqual(report.requests_with_anchor, "1/2")

    def test_request_identity_counts_tiers_and_verification(self):
        report = coverage(
            [],
            [],
            tiers={"main": 1, "subagent:abc": 2},
            checked_calls=2,
            disagreements=[{"request_id": "r", "run_seqs": [2],
                            "span_seqs": [2, 3]}],
            ungroupable=[{"seq": 1}, {"seq": 2}],
        )

        self.assertEqual(report.requests_with_anchor, "0/0")
        identity = report.request_identity
        self.assertEqual(identity["tier1_sessions"], 1)
        self.assertEqual(identity["tier2_sessions"], 1)
        self.assertEqual(identity["tier3_ungroupable_sessions"], 0)
        self.assertEqual(
            identity["derived_verification"],
            {"rule": "positional: user opens a turn; positive usage closes the span",
             "checked_calls": 2, "disagreements": 1},
        )
        self.assertEqual(identity["ungroupable_records"], 2)

    def test_bucket_sources_and_unknown_channels_grouped_by_channel(self):
        items = [
            item("main:1:0", "unknown", "qoder:attachment:mystery", tokens=4),
            item("main:2:0", "unknown", "qoder:attachment:mystery", tokens=4),
            item("main:3:0", "unknown", "qoder:attachment:new_thing", tokens=6),
            item("main:4:0", "user_message", "qoder:user", tokens=9),
        ]
        report = coverage(
            [], items, extraction=Extraction(envelope=12, signature=3, conflicts=1)
        )

        self.assertEqual(report.bucket_sources,
                         {"envelope": 12, "signature": 3,
                          "envelope_vs_signature_conflicts": 1})
        self.assertEqual(report.unknown_channels, [
            {"channel": "qoder:attachment:mystery", "count": 2, "tokens_est": 8},
            {"channel": "qoder:attachment:new_thing", "count": 1, "tokens_est": 6},
        ])

    def test_dropped_records_sorted_and_notes_passed_through(self):
        report = coverage(
            [],
            [],
            dropped={"non_dict": 1, "malformed_json": 2},
            notes=["corpus: all qoder sessions", "snapshot 2026-10-08"],
        )

        self.assertEqual(list(report.dropped_records), ["malformed_json", "non_dict"])
        self.assertEqual(report.notes,
                         ["corpus: all qoder sessions", "snapshot 2026-10-08"])


if __name__ == "__main__":
    unittest.main()
