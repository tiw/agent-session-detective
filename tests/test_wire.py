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
            "model": "qoder-model",
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
        self.assertIsNone(report_data["compaction_count"])
        self.assertIn("compactions: unavailable", report)
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
