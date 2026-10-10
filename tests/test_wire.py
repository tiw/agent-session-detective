import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import agent_session_detective
from agent_session_detective.ir.builder import build_audit_document
from agent_session_detective.report import render_report
from agent_session_detective.timeline import build_timeline, estimate_tokens
from agent_session_detective.tokenstats import BUCKET_KEYS, build_token_stats
from agent_session_detective.web import timeline_to_dict, tokenstats_to_dict
from agent_session_detective.wire import _detect_format, find_latest_qoder_transcript, load_session


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
                "RecordDropped",
                "RecordDropped",
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

    def test_session_starts_with_no_ide_db_stats(self):
        self.assertIsNone(self.session.ide_db_stats)

    def test_preserves_timestamp_source_and_jsonl_line_numbers(self):
        if not self.session.events:
            self.fail("Qoder transcript produced no events")
        expected_ts = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.session.events[0].ts, expected_ts)
        self.assertEqual(self.session.events[0].source, self.source)
        self.assertEqual([event.seq for event in self.session.events], [1, 2, 2, 2, 2, 2, 3, 3, 4, 5])

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
            "parent_tool_use_id": None,
            "request_id": None, "request_hash": None, "response_hash": None,
            "is_compact_summary": False, "human_text": None,
        })
        assistant_ref = {
            "uuid": "a1", "parent_uuid": "u1", "is_sidechain": False,
            "parent_tool_use_id": None,
            "request_id": "req-1", "request_hash": "r" * 64, "response_hash": "s" * 64,
            "is_compact_summary": False, "human_text": None,
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

    def test_dropped_records_surface_with_reasons_instead_of_vanishing(self):
        records = [
            {"type": "user", "uuid": "u0", "timestamp": "2026-10-07T08:00:00.000Z",
             "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "last-prompt", "uuid": "lp1",
             "timestamp": "2026-10-07T08:00:01.000Z"},
            {"type": "user", "uuid": "u2", "timestamp": "2026-10-07T08:00:02.000Z",
             "message": "not a dict"},
            {"type": "assistant", "uuid": "a2", "timestamp": "2026-10-07T08:00:03.000Z",
             "message": {"content": 5}},
        ]
        raw = "\n".join(json.dumps(record) for record in records)
        raw += '\n{not json\n"just a string"\n'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text(raw, encoding="utf-8")
            events = load_session(source).events

        dropped = [event for event in events if event.type == "RecordDropped"]
        self.assertEqual(
            [event.payload["reason"] for event in dropped],
            [
                "record:last-prompt",
                "message_invalid",
                "content_invalid",
                "malformed_json",
                "non_dict",
            ],
        )
        self.assertEqual([event.seq for event in dropped], [2, 3, 4, 5, 6])
        # Records that exist as JSON objects keep their timestamp and ref …
        self.assertTrue(all(event.ts is not None for event in dropped[:3]))
        self.assertEqual(
            [event.ref["uuid"] for event in dropped[:3]], ["lp1", "u2", "a2"]
        )
        # … while unparseable / non-object lines carry neither.
        for event in dropped[3:]:
            self.assertIsNone(event.ts)
            self.assertIsNone(event.ref)

    def test_user_record_flags_itself_as_compact_summary(self):
        records = [
            {"type": "user", "uuid": "u1", "isCompactSummary": True,
             "timestamp": "2026-10-07T08:00:00.000Z",
             "message": {"content": [{"type": "text", "text": "summary of prior work"}]}},
        ]
        events = self._load_records(records)

        self.assertEqual([event.type for event in events], ["TurnBegin"])
        self.assertTrue(events[0].ref["is_compact_summary"])
        self.assertEqual(
            events[0].payload,
            {"user_input": [{"type": "text", "text": "summary of prior work"}]},
        )

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

    def test_subagent_compactions_do_not_enter_the_main_timeline(self):
        main_messages = [
            {"timestamp": 1, "message": {"type": "CompactionBegin", "payload": {}}},
            {"timestamp": 2, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "first request"}],
            }}},
            {"timestamp": 3, "message": {"type": "TurnBegin", "payload": {
                "user_input": [{"type": "text", "text": "second request"}],
            }}},
        ]
        sub_messages = [
            {"timestamp": 1.5, "message": {"type": "CompactionBegin", "payload": {}}},
            {"timestamp": 1.6, "message": {"type": "CompactionEnd", "payload": {}}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "workspace" / "session"
            sub_dir = session_dir / "subagents" / "agent-x"
            sub_dir.mkdir(parents=True)
            (session_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in main_messages), encoding="utf-8"
            )
            (sub_dir / "wire.jsonl").write_text(
                "\n".join(json.dumps(message) for message in sub_messages), encoding="utf-8"
            )
            session = load_session(session_dir)

        timeline = build_timeline(session)

        # A subagent compacts its own context window; the main timeline counts
        # main-agent compactions only, or compaction_count inflates and
        # turn_growth wrongly marks turns as compaction-crossed.
        self.assertEqual(len(timeline.compactions), 1)
        self.assertEqual(timeline.compactions[0].begin_ts, 1)

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

    def test_qoder_usage_records_reconstruct_per_turn_context(self):
        # Billing-only harness: the full prompt of a turn's first request
        # (input + cache read + cache creation) IS the context at that
        # turn's start, so per-request usage records reconstruct the series.
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00Z",
             "message": {"content": "first request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:00:01Z", "message": {
                "content": "first reply",
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 800,
                          "cache_creation_input_tokens": 100, "output_tokens": 20},
            }},
            {"type": "user", "timestamp": "2026-10-07T08:01:00Z",
             "message": {"content": "second request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:01:01Z", "message": {
                "content": "second reply",
                "usage": {"input_tokens": 200, "cache_read_input_tokens": 1300,
                          "cache_creation_input_tokens": 0, "output_tokens": 20},
            }},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = load_session(source)

        stats = build_token_stats(session, build_timeline(session))

        self.assertEqual(len(stats.turn_growth), 2)
        self.assertEqual([row.context_at_start for row in stats.turn_growth], [1000, 1500])
        self.assertEqual([row.exact for row in stats.turn_growth], [True, True])
        self.assertEqual([row.added for row in stats.turn_growth], [500, None])
        self.assertNotEqual(stats.growth_verdict, "unavailable (no context telemetry)")
        self.assertTrue(stats.bucket_totals)

    def test_qoder_turn_without_usage_carries_forward_last_prompt(self):
        records = [
            {"type": "user", "timestamp": "2026-10-07T08:00:00Z",
             "message": {"content": "first request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:00:01Z", "message": {
                "content": "first reply",
                "usage": {"input_tokens": 400, "cache_read_input_tokens": 600,
                          "cache_creation_input_tokens": 0, "output_tokens": 20},
            }},
            {"type": "user", "timestamp": "2026-10-07T08:01:00Z",
             "message": {"content": "second request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:01:01Z",
             "message": {"content": "second reply (usage record missing)"}},
            {"type": "user", "timestamp": "2026-10-07T08:02:00Z",
             "message": {"content": "third request"}},
            {"type": "assistant", "timestamp": "2026-10-07T08:02:01Z", "message": {
                "content": "third reply",
                "usage": {"input_tokens": 500, "cache_read_input_tokens": 1500,
                          "cache_creation_input_tokens": 0, "output_tokens": 20},
            }},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            session = load_session(source)

        rows = build_token_stats(session, build_timeline(session)).turn_growth

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1].context_at_start, 1000)
        self.assertFalse(rows[1].exact)
        self.assertEqual(rows[1].added, 1000)

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


class GrowthChartSuppressionTests(unittest.TestCase):
    """The per-turn growth chart is skipped whenever no turn has a comparable
    delta. Skipping used to be silent, so a report showed only the
    "insufficient data" badge and never said a chart existed and why it was
    gone. These assert the reason is rendered instead."""

    def _governance(self, records):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
            session = load_session(source)
        timeline = build_timeline(session)
        stats = build_token_stats(session, timeline)
        report = render_report(
            session, timeline, [], judge_enabled=False, catalog_size=0, token_stats=stats
        )
        return report.split("<h2>Token Governance</h2>", 1)[1].split("<h2>", 1)[0], stats

    @staticmethod
    def _turn(seq, prompt_tokens, cache_read):
        return [
            {"type": "user", "timestamp": "2026-10-07T08:%02d:00Z" % seq,
             "message": {"content": "request %d" % seq}},
            {"type": "assistant", "timestamp": "2026-10-07T08:%02d:01Z" % seq, "message": {
                "content": "reply %d" % seq,
                "usage": {"input_tokens": prompt_tokens, "cache_read_input_tokens": cache_read,
                          "cache_creation_input_tokens": 0, "output_tokens": 20},
            }},
        ]

    def test_all_turns_unplottable_states_the_reason(self):
        records = self._turn(0, 100, 800) + [
            {"type": "system", "subtype": "compact_boundary", "uuid": "cb1",
             "timestamp": "2026-10-07T08:00:30Z",
             "compactMetadata": {"trigger": "auto", "preTokens": 900, "postTokens": 40}},
        ] + self._turn(1, 200, 300)
        section, stats = self._governance(records)

        self.assertEqual(stats.bucket_shares, {})
        self.assertTrue(stats.turn_growth[0].crossed_compaction)
        self.assertIn("no per-turn growth chart: 0 of 2 main-agent turns plottable", section)
        self.assertIn("1 crossed a compaction", section)
        self.assertIn("1 is the last turn", section)
        self.assertNotIn("<svg", section)

    def test_single_plottable_turn_states_the_two_turn_minimum(self):
        section, stats = self._governance(self._turn(0, 100, 800) + self._turn(1, 200, 1300))

        self.assertTrue(stats.bucket_shares)
        self.assertIn("no per-turn growth chart: 1 plottable turn", section)
        self.assertIn("at least 2 are needed", section)
        # the bucket table still carries the one turn's attribution
        self.assertIn("<th>tokens added</th>", section)

    def test_plottable_turns_render_the_chart_without_a_gap_note(self):
        records = (self._turn(0, 100, 800) + self._turn(1, 200, 1300)
                   + self._turn(2, 300, 1800))
        section, _ = self._governance(records)

        self.assertIn("<svg", section)
        self.assertNotIn("no per-turn growth chart", section)


class SkillLoadRenderTests(unittest.TestCase):
    """Spec Tests #2/#8 — render regressions on both surfaces.

    The session carries one evidence-backed load (Skill stub + hook_output
    body) and one marker-only stub with no body, so every surface has both
    a costable row and an "unavailable" row to render."""

    BODY = '<skill_content name="demo">\nSteps of the demo skill\n</skill_content>'

    def _session(self):
        def rec(kind, uuid, parent, ts, **extra):
            record = {
                "type": kind, "uuid": uuid, "parentUuid": parent,
                "isSidechain": False,
                "timestamp": "2026-10-07T08:00:%02d.000Z" % ts,
            }
            record.update(extra)
            return record

        records = [
            rec("user", "u1", None, 0,
                message={"content": [{"type": "text", "text": "go"}]}),
            rec("assistant", "a1", "u1", 1,
                requestTokenAnchor={"request": "a" * 16, "response": "b" * 16,
                                    "requestId": "req-1"},
                message={"model": "qoder-pro",
                         "content": [{"type": "tool_use", "id": "t1",
                                      "name": "Skill",
                                      "input": {"skill": "demo"}}],
                         "usage": {"input_tokens": 1000, "output_tokens": 10}}),
            rec("user", "u2", "a1", 2,
                message={"content": [{"type": "tool_result",
                                      "tool_use_id": "t1",
                                      "content": "Launching skill: demo"}]}),
            rec("attachment", "u3", "u2", 3,
                attachment={"type": "hook_output", "output": self.BODY}),
            rec("assistant", "a2", "u3", 4,
                requestTokenAnchor={"request": "c" * 16, "response": "d" * 16,
                                    "requestId": "req-2"},
                message={"model": "qoder-pro",
                         "content": [{"type": "tool_use", "id": "t2",
                                      "name": "Skill",
                                      "input": {"skill": "ghost"}}],
                         "usage": {"input_tokens": 1100, "output_tokens": 10}}),
            rec("user", "u4", "a2", 5,
                message={"content": [{"type": "tool_result",
                                      "tool_use_id": "t2",
                                      "content": "Launching skill: ghost"}]}),
            rec("assistant", "a3", "u4", 6,
                requestTokenAnchor={"request": "e" * 16, "response": "f" * 16,
                                    "requestId": "req-3"},
                message={"model": "qoder-pro",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 1200, "output_tokens": 10}}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "transcript.jsonl"
            source.write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            return load_session(source)

    def _render(self, document):
        session = self._session()
        timeline = build_timeline(session)
        stats = build_token_stats(session, timeline)
        report = render_report(
            session, timeline, [], judge_enabled=False, catalog_size=0,
            token_stats=stats, document=document,
        )
        return report, timeline

    def test_lifecycle_renders_marker_only_and_the_table_carries_costs(self):
        document = build_audit_document(self._session(), "qoder")
        bases = {row.skill_id: row.cost_basis for row in document.skill_loads}
        self.assertEqual(bases, {"demo": "body", "ghost": "unavailable"})

        report, _ = self._render(document)
        lifecycle = report.split("<h2>Skill Lifecycle</h2>", 1)[1].split(
            "<h2>", 1
        )[0]
        evidence = report.split(
            "<h2>Skill loads (evidence-backed)</h2>", 1
        )[1].split("<h2>", 1)[0]

        # lifecycle: the marker label, both loads, and no cost anywhere
        self.assertIn("load marker", lifecycle)
        self.assertIn("demo", lifecycle)
        self.assertIn("ghost", lifecycle)
        self.assertIsNone(re.search(r"~\d", lifecycle))
        self.assertNotIn("(EST)", lifecycle)

        # evidence table: the body-derived cost labeled EST, the stub-only
        # row labeled unavailable — and no other cost number in the section
        est = "~%d (EST)" % estimate_tokens(self.BODY)
        self.assertIn(est, evidence)
        self.assertEqual(re.findall(r"~\d+", evidence), [est[:-6]])
        self.assertIn("unavailable", evidence)
        # totals line carries counts, not costs
        self.assertIn(
            "totals: 2 load(s), 1 with body, 1 unavailable, 0 reload(s), "
            "0 redundant body(ies)",
            evidence,
        )

    def test_without_a_document_no_cost_number_renders_anywhere(self):
        report, _ = self._render(None)

        # non-Qoder / pre-IR surfaces still get the honest marker-only label
        self.assertIn("load marker", report)
        self.assertNotIn("Skill loads (evidence-backed)", report)
        self.assertNotIn("(EST)", report)
        self.assertIsNone(re.search(r"~\d", report))

    def test_webapp_lifecycle_line_carries_no_cost_field(self):
        app_js = (
            Path(agent_session_detective.__file__).parent / "webapp" / "app.js"
        ).read_text(encoding="utf-8")

        # the lifecycle loads map must not touch the stub-derived estimate
        self.assertNotIn("l.tokens_est", app_js)
        # the loads table is the only cost renderer, labeled EST/unavailable
        self.assertIn('row.cost_basis === "body"', app_js)
        self.assertIn('"~" + row.cost_tokens_est + " (EST)"', app_js)
        self.assertIn("unavailable", app_js)


class QoderTerminalCliTranscriptTests(unittest.TestCase):
    """The Qoder terminal CLI stores a middleweight transcript under
    <workspace>/transcript/<uuid>.jsonl: session_meta/progress/user/assistant
    records with no usage telemetry and no envelope. It and the codex
    rollout format both lead with a session_meta record — codex carries a
    ``payload`` dict, the qoder CLI a ``data`` dict — so detection must
    tell them apart. The CLI transcript loads degraded: the conversation
    and tool calls reconstruct, and the session is stamped so token
    costs stay unavailable."""

    CLI_RECORDS = [
        {
            "type": "session_meta",
            "sessionId": "b1d65022-d35d-4a45-b24f-27eb970e6b86",
            "uuid": "97374e82-4c47-4d60-b51f-b0b32e07d0aa",
            "timestamp": "2026-10-09T03:03:13.471444Z",
            "cwd": "/Users/wangting/work/paze-test",
            "data": {
                "meta_type": "slash_command",
                "content": {
                    "name": "paze-code-workflow",
                    "type": "skill",
                    "filePath": "/Users/wangting/.qoder/skills/paze-code-workflow/SKILL.md",
                },
            },
        },
        {
            "type": "user",
            "sessionId": "b1d65022-d35d-4a45-b24f-27eb970e6b86",
            "uuid": "1f0a2a3b-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
            "timestamp": "2026-10-09T03:03:20.000Z",
            "cwd": "/Users/wangting/work/paze-test",
            "message": {"role": "user", "content": [{"type": "text", "text": "run the workflow"}]},
        },
    ]

    def _write_cli_transcript(self, directory: Path) -> Path:
        source = directory / "b1d65022-d35d-4a45-b24f-27eb970e6b86.jsonl"
        source.write_text(
            "\n".join(json.dumps(record) for record in self.CLI_RECORDS), encoding="utf-8"
        )
        return source

    def test_detects_qoder_terminal_cli_format(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_cli_transcript(Path(directory))
            self.assertEqual(_detect_format(source), "qoder-cli")

    def test_codex_rollout_still_detects_as_codex(self):
        self.assertEqual(_detect_format(FIXTURES / "codex-session.jsonl"), "codex")

    def test_load_session_renders_terminal_cli_transcript_degraded(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write_cli_transcript(Path(directory))
            session = load_session(source)
            self.assertEqual(session.source_format, "qoder-cli")
            self.assertFalse(session.compaction_telemetry_available)
            self.assertIn("TurnBegin", [event.type for event in session.events])

    def test_detects_old_terminal_cli_transcript_without_session_meta(self):
        # Pre-October CLI transcripts on disk have no session_meta leader:
        # they start directly with envelope-less assistant records and
        # interleave progress records (rich IDE transcripts never contain
        # progress records).
        records = [
            {
                "type": "assistant",
                "sessionId": "717bf171-9e3a-416b-9332-dbed1cd8670b",
                "uuid": "u1",
                "timestamp": "2026-08-05T07:06:46.258714Z",
                "cwd": "/w",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
            {
                "type": "progress",
                "sessionId": "717bf171-9e3a-416b-9332-dbed1cd8670b",
                "uuid": "u2",
                "timestamp": "2026-08-05T07:06:46.5Z",
                "cwd": "/w",
                "data": {"type": "hook_progress", "hookName": "PreToolUse:Read"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "old-cli.jsonl"
            source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            self.assertEqual(_detect_format(source), "qoder-cli")

    def test_load_session_renders_old_terminal_cli_transcript_degraded(self):
        records = [
            {
                "type": "assistant",
                "sessionId": "s1",
                "uuid": "u1",
                "timestamp": "2026-08-05T07:06:46Z",
                "cwd": "/w",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
            {
                "type": "progress",
                "sessionId": "s1",
                "uuid": "u2",
                "timestamp": "2026-08-05T07:06:46.5Z",
                "cwd": "/w",
                "data": {"type": "hook_progress"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "old-cli.jsonl"
            source.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
            session = load_session(source)
            self.assertEqual(session.source_format, "qoder-cli")
            self.assertFalse(session.compaction_telemetry_available)
            self.assertIn("ContentPart", [event.type for event in session.events])

    def test_rich_ide_transcript_detects_as_qoder(self):
        # Rich IDE user/assistant records carry the parentUuid/origin
        # envelope chain; the CLI never writes those fields.
        records = [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-10-07T08:00:00.000Z",
                "parentUuid": None,
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rich.jsonl"
            source.write_text(json.dumps(records[0]), encoding="utf-8")
            self.assertEqual(_detect_format(source), "qoder")


if __name__ == "__main__":
    unittest.main()
