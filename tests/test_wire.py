import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_session_detective.report import render_report
from agent_session_detective.timeline import build_timeline, estimate_tokens
from agent_session_detective.tokenstats import BUCKET_KEYS, build_token_stats
from agent_session_detective.web import timeline_to_dict, tokenstats_to_dict
from agent_session_detective.wire import find_latest_qoder_transcript, load_session


FIXTURES = Path(__file__).parent / "fixtures"


class QoderTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.source = FIXTURES / "qoder-transcript.jsonl"
        self.session = load_session(self.source)

    def test_loads_qoder_transcript_into_existing_event_model(self):
        self.assertEqual(
            [event.type for event in self.session.events],
            [
                "TurnBegin",
                "ContentPart",
                "ContentPart",
                "ToolCall",
                "ToolCall",
                "UsageRecord",
                "ToolResult",
                "ToolResult",
            ],
        )
        self.assertEqual(self.session.events[0].payload, {"user_input": [{"type": "text", "text": "audit this"}]})
        self.assertEqual(self.session.events[1].payload, {"type": "text", "text": "I will inspect it."})
        self.assertEqual(self.session.events[2].payload, {"type": "think", "think": "inspect trace"})
        self.assertEqual(self.session.events[3].payload, {
            "id": "tool-skill",
            "function": {
                "name": "Skill",
                "arguments": json.dumps({"skill": "superpowers:brainstorming"}),
            },
        })
        self.assertEqual(self.session.events[6].payload, {
            "tool_call_id": "tool-skill",
            "return_value": {"output": "skill instructions", "is_error": False},
        })
        self.assertEqual(self.session.events[5].payload, {
            "input_other": 120,
            "output": 30,
            "input_cache_read": 80,
            "input_cache_creation": 0,
            "input_cache_creation_5m": 0,
            "input_cache_creation_1h": 0,
            "model": "qoder-model",
            # Telemetry this fixture's record does not carry stays absent rather
            # than being back-filled with a plausible default.
            "context_usage_ratio": None,
            "request_id": None,
            "credits": None,
            "original_credits": None,
            "billable": None,
        })

    def test_preserves_timestamp_source_and_jsonl_line_numbers(self):
        if not self.session.events:
            self.fail("Qoder transcript produced no events")
        expected_ts = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.session.events[0].ts, expected_ts)
        self.assertEqual(self.session.events[0].source, self.source)
        self.assertEqual([event.seq for event in self.session.events], [1, 2, 2, 2, 2, 2, 3, 3])

    def test_normalizes_scalar_message_content_as_text(self):
        records = [
            {"type": "user", "message": {"content": "user request"}},
            {"type": "assistant", "message": {"content": "assistant response"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            events = load_session(source).events

        self.assertEqual(
            [(event.type, event.payload) for event in events],
            [
                ("TurnBegin", {"user_input": [{"type": "text", "text": "user request"}]}),
                ("ContentPart", {"type": "text", "text": "assistant response"}),
            ],
        )

    def test_preserves_tool_result_error_flag(self):
        record = {
            "type": "user",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "command failed",
                "is_error": True,
            }]},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text(json.dumps(record), encoding="utf-8")
            events = load_session(source).events

        self.assertEqual(events[0].payload["return_value"]["is_error"], True)

    def test_retains_compact_boundary_record_with_harness_measured_facts(self):
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00.000Z",
             "message": {"content": [{"type": "text", "text": "keep going"}]}},
            {
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "cb1",
                "logicalParentUuid": "a9",
                "timestamp": "2026-10-07T08:00:05.919Z",
                "content": "Conversation compacted",
                "level": "info",
                "isSidechain": False,
                "compactMetadata": {
                    "trigger": "auto",
                    "preTokens": 231067,
                    "messagesSummarized": 612,
                    "postTokens": 3247,
                    "durationMs": 32122,
                },
            },
        ]
        events = self._load_records(records)

        compactions = [event for event in events if event.type == "CompactionBegin"]
        self.assertEqual(len(compactions), 1)
        self.assertEqual(compactions[0].payload, {
            "trigger": "auto",
            "pre_tokens": 231067,
            "post_tokens": 3247,
            "messages_summarized": 612,
            "duration_ms": 32122,
            "logical_parent_uuid": "a9",
        })
        self.assertEqual(compactions[0].ref["uuid"], "cb1")
        self.assertEqual(compactions[0].ref["is_sidechain"], False)
        self.assertEqual(
            compactions[0].ts,
            datetime(2026, 10, 7, 8, 0, 5, 919000, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(compactions[0].seq, 2)

    def test_retains_runtime_config_record_with_epoch_millisecond_timestamp(self):
        records = [
            {"type": "runtime-config", "sessionId": "s1", "model": "performance",
             "contextWindow": 272000, "reasoningEffort": None, "timestamp": 1791422285078},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["RuntimeConfig"])
        self.assertEqual(events[0].payload, {"model": "performance", "context_window": 272000})
        self.assertEqual(events[0].ts, 1791422285.078)

    def test_retains_attachment_records_with_untruncated_body_and_identity(self):
        body = "- mcp-config: configure MCP servers.\n" + ("x" * 30000)
        records = [
            {
                "type": "attachment",
                "uuid": "at1",
                "parentUuid": "p1",
                "isSidechain": False,
                "timestamp": "2026-10-07T08:00:01.000Z",
                "version": "1.1.64",
                "sessionId": "s1",
                "attachment": {
                    "type": "skill_listing",
                    "content": body,
                    "names": ["mcp-config"],
                    "skillCount": 1,
                    "isInitial": True,
                },
            },
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["Attachment"])
        self.assertEqual(events[0].payload["attachment_type"], "skill_listing")
        self.assertEqual(events[0].payload["attachment"]["content"], body)
        self.assertEqual(events[0].ref["uuid"], "at1")
        self.assertEqual(events[0].ref["parent_uuid"], "p1")
        self.assertEqual(events[0].ref["is_sidechain"], False)
        self.assertEqual(events[0].seq, 1)

    def test_retains_attachment_records_of_unknown_type(self):
        records = [
            {"type": "attachment", "uuid": "at2", "timestamp": "2026-10-07T08:00:02.000Z",
             "attachment": {"type": "brand_new_kind", "payload": {"a": 1}}},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["Attachment"])
        self.assertEqual(events[0].payload["attachment_type"], "brand_new_kind")
        self.assertEqual(events[0].payload["attachment"], {"type": "brand_new_kind", "payload": {"a": 1}})
        self.assertIsNone(events[0].ref["parent_uuid"])

    def test_retains_active_leaf_record_for_storage_without_interpretation(self):
        records = [
            {"type": "active-leaf", "sessionId": "s1", "leafUuid": "leaf-9",
             "explicit": True, "timestamp": 1791422287303},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["ActiveLeaf"])
        self.assertEqual(events[0].payload, {"leaf_uuid": "leaf-9", "explicit": True})
        self.assertEqual(events[0].ts, 1791422287.303)

    def test_retains_non_compaction_system_records_as_notices(self):
        records = [
            {"type": "system", "subtype": "informational", "level": "info",
             "content": "Goal set: support qoder cli | Max turns: 100",
             "uuid": "n1", "parentUuid": None, "isSidechain": False,
             "timestamp": "2026-09-24T03:30:18.831Z"},
            {"type": "system", "subtype": "api_retry", "level": "error",
             "content": "Empty assistant completion", "error": "empty_response",
             "error_status": None, "attempt": 1, "max_retries": 2, "retry_delay_ms": 595,
             "uuid": "n2", "parentUuid": "cc18", "isSidechain": False,
             "timestamp": "2026-09-24T04:02:23.906Z"},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["SystemNotice", "SystemNotice"])
        self.assertEqual(events[0].payload["subtype"], "informational")
        self.assertEqual(events[0].payload["content"], "Goal set: support qoder cli | Max turns: 100")
        self.assertEqual(events[0].payload["level"], "info")
        self.assertIsNone(events[0].payload["attempt"])
        self.assertEqual(events[1].payload["subtype"], "api_retry")
        self.assertEqual(events[1].payload["error"], "empty_response")
        self.assertEqual(events[1].payload["attempt"], 1)
        self.assertEqual(events[1].payload["max_retries"], 2)
        self.assertEqual(events[1].payload["retry_delay_ms"], 595)
        self.assertEqual(events[1].ref["parent_uuid"], "cc18")

    def test_qoder_session_declares_compaction_telemetry_available(self):
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00.000Z",
             "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "system", "subtype": "compact_boundary", "uuid": "cb1",
             "timestamp": "2026-10-07T08:00:05.000Z",
             "compactMetadata": {"trigger": "auto", "preTokens": 900, "postTokens": 40}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            session = load_session(source)
            timeline = build_timeline(session)

        self.assertTrue(session.compaction_telemetry_available)
        self.assertTrue(timeline.compaction_telemetry_available)
        self.assertEqual(len(timeline.compactions), 1)

    def test_captures_provider_telemetry_on_assistant_usage_records(self):
        records = [
            {"type": "assistant", "timestamp": "2026-10-07T08:00:02.000Z",
             "message": {
                 "model": "qoder-model",
                 "content": [{"type": "text", "text": "done"}],
                 "usage": {
                     "input_tokens": 29835,
                     "cache_creation_input_tokens": 1500,
                     "cache_read_input_tokens": 4800,
                     "output_tokens": 1248,
                     "cache_creation": {"ephemeral_5m_input_tokens": 1200,
                                        "ephemeral_1h_input_tokens": 300},
                     "credits": 0.5861435914285714,
                     "original_credits": 0.6,
                     "billable": True,
                     "request_id": "req-1",
                     "context_usage_ratio": 0.2330859375,
                 },
             }},
        ]
        events = self._load_records(records)

        usage = [event for event in events if event.type == "UsageRecord"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].payload, {
            "input_other": 29835,
            "output": 1248,
            "input_cache_read": 4800,
            "input_cache_creation": 1500,
            "input_cache_creation_5m": 1200,
            "input_cache_creation_1h": 300,
            "model": "qoder-model",
            "context_usage_ratio": 0.2330859375,
            "request_id": "req-1",
            "credits": 0.5861435914285714,
            "original_credits": 0.6,
            "billable": True,
        })

    def test_attaches_source_record_identity_to_every_event_from_that_record(self):
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00.000Z",
             "uuid": "u1", "parentUuid": None, "isSidechain": False,
             "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "assistant", "timestamp": "2026-10-07T08:00:02.000Z",
             "uuid": "a1", "parentUuid": "u1", "isSidechain": False,
             "requestTokenAnchor": {"request": "r" * 64, "response": "s" * 64,
                                    "requestId": "req-1"},
             "message": {
                 "model": "qoder-model",
                 "content": [
                     {"type": "text", "text": "working"},
                     {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                 ],
                 "usage": {"input_tokens": 10, "output_tokens": 5, "request_id": "req-1"},
             }},
            # A continuation of the same request that carries no usage at all:
            # its anchor is the only thing that ties it back to req-1.
            {"type": "assistant", "timestamp": "2026-10-07T08:00:03.000Z",
             "uuid": "a2", "parentUuid": "a1", "isSidechain": False,
             "requestTokenAnchor": {"request": "r" * 64, "response": "s" * 64,
                                    "requestId": "req-1"},
             "message": {"model": "qoder-model",
                         "content": [{"type": "text", "text": "more"}]}},
        ]
        events = self._load_records(records)

        self.assertEqual(
            [event.type for event in events],
            ["TurnBegin", "ContentPart", "ToolCall", "UsageRecord", "ContentPart"],
        )
        self.assertEqual(events[0].ref, {
            "uuid": "u1", "parent_uuid": None, "is_sidechain": False,
            "request_id": None, "request_hash": None, "response_hash": None,
        })
        assistant_ref = {
            "uuid": "a1", "parent_uuid": "u1", "is_sidechain": False,
            "request_id": "req-1", "request_hash": "r" * 64, "response_hash": "s" * 64,
        }
        for event in events[1:4]:
            self.assertEqual(event.ref, assistant_ref)
        self.assertEqual(events[4].ref["request_id"], "req-1")
        self.assertEqual(events[4].ref["uuid"], "a2")
        self.assertEqual(events[4].ref["parent_uuid"], "a1")

    def test_falls_back_to_usage_request_id_when_no_token_anchor_is_present(self):
        # Qoder only began writing requestTokenAnchor in 1.1.58. Older
        # transcripts still name the request, but only inside the usage block.
        records = [
            {"type": "assistant", "timestamp": "2026-08-01T00:00:00.000Z",
             "version": "1.1.37", "uuid": "a1", "parentUuid": "u1",
             "message": {"model": "m",
                         "content": [{"type": "text", "text": "hi"}],
                         "usage": {"input_tokens": 7, "output_tokens": 2,
                                   "request_id": "req-old"}}},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["ContentPart", "UsageRecord"])
        self.assertEqual([event.ref["request_id"] for event in events], ["req-old", "req-old"])
        self.assertIsNone(events[0].ref["request_hash"])
        self.assertIsNone(events[0].ref["response_hash"])

    def _load_records(self, records):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            return load_session(source).events

    def test_timeline_detects_skill_and_skill_file_read(self):
        timeline = build_timeline(self.session)
        self.assertEqual(timeline.skill_names(), ["superpowers:brainstorming"])
        self.assertEqual([read.path for read in timeline.file_reads], ["/tmp/demo/SKILL.md"])
        self.assertEqual([read.skill_name for read in timeline.file_reads], ["demo"])

    def test_finds_newest_qoder_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            older = base / "project-a" / "older.jsonl"
            newest = base / "project-b" / "newest.jsonl"
            older.parent.mkdir()
            newest.parent.mkdir()
            older.write_text("{}", encoding="utf-8")
            newest.write_text("{}", encoding="utf-8")
            older.touch()
            newest.touch()

            self.assertEqual(find_latest_qoder_transcript(base), newest)

    def test_kimi_context_measurements_still_produce_turn_growth(self):
        messages = [
            {"timestamp": 0.5, "message": {"type": "StatusUpdate", "payload": {
                "context_tokens": 100,
            }}},
            {"timestamp": 1, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "first request"}],
            }}},
            {"timestamp": 1.5, "message": {"type": "StatusUpdate", "payload": {
                "context_tokens": 150,
            }}},
            {"timestamp": 2, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "second request"}],
            }}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "workspace" / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in messages), encoding="utf-8"
            )
            session = load_session(session_dir)

        timeline = build_timeline(session)
        stats = build_token_stats(session, timeline)

        self.assertEqual([row.added for row in stats.turn_growth], [50, None])
        self.assertTrue(stats.bucket_totals)
        self.assertEqual(timeline_to_dict(timeline)["compaction_count"], 0)

    def test_bucket_split_separates_skill_inject_and_tool(self):
        inject = "<system-reminder>The user selected these skills: brainstorming.</system-reminder>"
        human = "audit this trace"
        skill_body = '<skill_content name="superpowers:brainstorming">explore the design space</skill_content>'
        messages = [
            {"timestamp": 0.5, "message": {"type": "StatusUpdate", "payload": {
                "context_tokens": 1000,
            }}},
            {"timestamp": 1, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": inject + human}],
            }}},
            {"timestamp": 1.2, "message": {"type": "ToolCall", "payload": {
                "id": "call-skill",
                "function": {"name": "Skill", "arguments": '{"skill": "superpowers:brainstorming"}'},
            }}},
            {"timestamp": 1.3, "message": {"type": "ToolResult", "payload": {
                "tool_call_id": "call-skill",
                "return_value": {"output": skill_body, "is_error": False},
            }}},
            {"timestamp": 1.4, "message": {"type": "ToolResult", "payload": {
                "tool_call_id": "call-bash",
                "return_value": {"output": "README.md src tests", "is_error": False},
            }}},
            {"timestamp": 1.6, "message": {"type": "ContentPart", "payload": {
                "type": "text", "text": "here is what I found",
            }}},
            {"timestamp": 2, "message": {"type": "StatusUpdate", "payload": {
                "context_tokens": 5000,
            }}},
            {"timestamp": 2.5, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "next"}],
            }}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "workspace" / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in messages), encoding="utf-8"
            )
            session = load_session(session_dir)

        stats = build_token_stats(session, build_timeline(session))
        row = stats.turn_growth[0]

        self.assertEqual(row.added, 4000)
        self.assertEqual(row.inject_added, estimate_tokens(inject))
        self.assertEqual(row.history_added, estimate_tokens(human))
        self.assertEqual(row.skill_added, estimate_tokens(skill_body))
        self.assertEqual(row.tool_added, estimate_tokens("README.md src tests"))
        self.assertEqual(row.output_added, estimate_tokens("here is what I found"))
        self.assertEqual(
            row.system_added,
            4000 - (row.inject_added + row.history_added + row.skill_added
                    + row.tool_added + row.output_added),
        )
        self.assertEqual(list(stats.bucket_totals), list(BUCKET_KEYS))
        self.assertEqual({it.bucket for it in row.items}, {"history", "inject", "skill", "tool", "output"})

        serialized = tokenstats_to_dict(stats)["turn_growth"][0]
        for key in BUCKET_KEYS:
            self.assertEqual(serialized[key + "_added"], getattr(row, key + "_added"))

    def test_skill_body_is_bucketed_by_content_signature_without_the_call_envelope(self):
        skill_body = '<skill_content name="demo">steps the harness forgot to tag</skill_content>'
        messages = [
            {"timestamp": 0.5, "message": {"type": "StatusUpdate", "payload": {"context_tokens": 100}}},
            {"timestamp": 1, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "go"}],
            }}},
            {"timestamp": 1.2, "message": {"type": "ToolResult", "payload": {
                "tool_call_id": "call-untracked",
                "return_value": {"output": skill_body, "is_error": False},
            }}},
            {"timestamp": 2, "message": {"type": "StatusUpdate", "payload": {"context_tokens": 900}}},
            {"timestamp": 2.5, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "next"}],
            }}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "workspace" / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in messages), encoding="utf-8"
            )
            session = load_session(session_dir)

        row = build_token_stats(session, build_timeline(session)).turn_growth[0]

        self.assertEqual(row.skill_added, estimate_tokens(skill_body))
        self.assertEqual(row.tool_added, 0)

    def test_qoder_without_context_telemetry_omits_context_derived_metrics(self):
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00Z", "message": {"content": "first request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:00:01Z", "message": {"content": "first reply"}},
            {"type": "user", "timestamp": "2026-10-07T08:01:00Z", "message": {"content": "second request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:01:01Z", "message": {"content": "second reply"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = load_session(source)

        timeline = build_timeline(session)
        stats = build_token_stats(session, timeline)
        report_data = timeline_to_dict(timeline)
        report = render_report(
            session, timeline, [], judge_enabled=False, catalog_size=0, token_stats=stats
        )

        self.assertEqual(stats.turn_growth, [])
        self.assertEqual(stats.bucket_totals, {})
        self.assertEqual(stats.bucket_shares, {})
        self.assertEqual(stats.hash_runs, [])
        self.assertEqual(stats.growth_verdict, "unavailable (no context telemetry)")
        self.assertIsNone(stats.hash_flips)
        self.assertEqual(
            tokenstats_to_dict(stats)["growth_verdict"], "unavailable (no context telemetry)"
        )
        self.assertIsNone(tokenstats_to_dict(stats)["hash_flips"])
        # Compaction is not a context-derived metric for Qoder: the transcript
        # records each boundary with harness-measured token counts, so an
        # absent compaction is the fact "zero", not "unknown".
        self.assertEqual(report_data["compaction_count"], 0)
        self.assertIn("compactions: 0", report)
        self.assertIn("growth: unavailable (no context telemetry)", report)
        self.assertIn("prompt flips: unavailable", report)

    def test_kimi_request_hashes_with_no_changes_report_zero_flips(self):
        messages = [
            {"timestamp": 1, "message": {"type": "LLMRequest", "payload": {
                "system_prompt_hash": "system-a", "tools_hash": "tools-a",
            }}},
            {"timestamp": 2, "message": {"type": "LLMRequest", "payload": {
                "system_prompt_hash": "system-a", "tools_hash": "tools-a",
            }}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "workspace" / "session"
            session_dir.mkdir(parents=True)
            (session_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in messages), encoding="utf-8"
            )
            session = load_session(session_dir)

        stats = build_token_stats(session, build_timeline(session))

        self.assertEqual(stats.hash_flips, 0)
        self.assertEqual(tokenstats_to_dict(stats)["hash_flips"], 0)


if __name__ == "__main__":
    unittest.main()
