import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli
from agent_session_detective.timeline import build_timeline
from agent_session_detective.tokenstats import build_token_stats
from agent_session_detective.web import discover_sessions, fingerprint
from agent_session_detective.wire import (
    find_latest_codex_session,
    load_session,
    parse_codex_session,
)

FIXTURES = Path(__file__).parent / "fixtures"


class CodexSessionParserTests(unittest.TestCase):
    """Test the Codex rollout JSONL parser."""

    def setUp(self):
        self.source = FIXTURES / "codex-session.jsonl"
        self.session = load_session(self.source)

    def test_loads_codex_session_into_event_model(self):
        """Codex session should produce TurnBegin, ContentPart, ToolCall, ToolResult, UsageRecord."""
        event_types = [e.type for e in self.session.events]
        self.assertIn("TurnBegin", event_types)
        self.assertIn("ContentPart", event_types)
        self.assertIn("ToolCall", event_types)
        self.assertIn("ToolResult", event_types)
        self.assertIn("UsageRecord", event_types)

    def test_parses_user_message_as_turn_begin(self):
        """User message (role=user) should produce TurnBegin."""
        turn_begins = [e for e in self.session.events if e.type == "TurnBegin"]
        self.assertTrue(len(turn_begins) > 0)
        first_turn = turn_begins[0]
        self.assertEqual(first_turn.payload["user_input"][0]["text"], "audit this session")

    def test_parses_assistant_text_as_content_part(self):
        """Assistant message (role=assistant, output_text) should produce ContentPart text."""
        content_parts = [
            e for e in self.session.events
            if e.type == "ContentPart" and e.payload.get("type") == "text"
        ]
        self.assertTrue(len(content_parts) > 0)
        self.assertEqual(content_parts[0].payload["text"], "Let me analyze this.")

    def test_parses_reasoning_as_content_part_think(self):
        """Reasoning (summary_text) should produce ContentPart think."""
        think_parts = [
            e for e in self.session.events
            if e.type == "ContentPart" and e.payload.get("type") == "think"
        ]
        self.assertTrue(len(think_parts) > 0)
        self.assertEqual(think_parts[0].payload["think"], "I should inspect the trace")

    def test_parses_function_call_as_tool_call(self):
        """function_call should produce ToolCall with call_id and arguments."""
        tool_calls = [e for e in self.session.events if e.type == "ToolCall"]
        self.assertTrue(len(tool_calls) >= 2)
        # First tool call should be Skill
        skill_call = next(tc for tc in tool_calls if tc.payload["function"]["name"] == "Skill")
        self.assertEqual(skill_call.payload["id"], "call-skill-001")
        self.assertIn("superpowers:brainstorming", skill_call.payload["function"]["arguments"])

    def test_parses_function_call_output_as_tool_result(self):
        """function_call_output should produce ToolResult with call_id and output."""
        tool_results = [e for e in self.session.events if e.type == "ToolResult"]
        self.assertTrue(len(tool_results) >= 2)
        skill_result = next(tr for tr in tool_results if tr.payload["tool_call_id"] == "call-skill-001")
        self.assertEqual(skill_result.payload["return_value"]["output"], "skill instructions loaded")
        self.assertFalse(skill_result.payload["return_value"]["is_error"])

    def test_parses_token_usage_record(self):
        """token_usage_record should produce UsageRecord with token counts."""
        usage_records = [e for e in self.session.events if e.type == "UsageRecord"]
        self.assertTrue(len(usage_records) > 0)
        usage = usage_records[0]
        # input_tokens=500, cached=200, so input_other=300
        self.assertEqual(usage.payload["input_other"], 300)
        self.assertEqual(usage.payload["input_cache_read"], 200)
        self.assertEqual(usage.payload["input_cache_creation"], 50)
        self.assertEqual(usage.payload["output"], 80)

    def test_skips_developer_role_messages(self):
        """Developer role messages (system instructions) should be skipped."""
        # The fixture has a developer message at ordinal 2
        # It should NOT produce a TurnBegin or ContentPart
        turn_begins = [e for e in self.session.events if e.type == "TurnBegin"]
        for tb in turn_begins:
            for part in tb.payload.get("user_input", []):
                self.assertNotIn("System instructions here", part.get("text", ""))

    def test_skips_malformed_json_lines(self):
        """Malformed JSON lines should be skipped without error."""
        # The fixture has a malformed line at the end
        # The parser should not raise an exception
        self.assertTrue(len(self.session.events) > 0)

    def test_preserves_timestamps(self):
        """Timestamps should be parsed from ISO format."""
        first_event = self.session.events[0]
        self.assertIsNotNone(first_event.ts)
        # 2026-10-07T10:00:00.000Z should parse to a valid timestamp
        expected_ts = datetime(2026, 10, 7, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(first_event.ts, expected_ts, delta=1.0)

    def test_preserves_source_and_seq(self):
        """Events should preserve source file path and line number."""
        for event in self.session.events:
            self.assertEqual(event.source, self.source)
            self.assertTrue(event.seq > 0)

    def test_timeline_detects_skill_load(self):
        """Timeline should detect Skill tool call as a skill load."""
        timeline = build_timeline(self.session)
        self.assertIn("superpowers:brainstorming", timeline.skill_names())

    def test_timeline_detects_skill_file_read(self):
        """Timeline should detect Read of SKILL.md as a file read."""
        timeline = build_timeline(self.session)
        file_reads = [fr for fr in timeline.file_reads if "SKILL.md" in fr.path]
        self.assertTrue(len(file_reads) > 0)
        self.assertEqual(file_reads[0].path, "/tmp/demo/SKILL.md")

    def test_token_stats_extract_usage(self):
        """Token stats should extract usage records from Codex session."""
        timeline = build_timeline(self.session)
        stats = build_token_stats(self.session, timeline)
        self.assertTrue(len(stats.usage_records) > 0)
        # input_total = input_other (300) + input_cache_read (200) + input_cache_creation (50) = 550
        self.assertEqual(stats.input_total, 550)
        self.assertEqual(stats.output_total, 80)

    def test_compaction_telemetry_not_available(self):
        """Codex sessions don't have compaction telemetry."""
        self.assertFalse(self.session.compaction_telemetry_available)


class CodexCliTests(unittest.TestCase):
    """Test CLI integration with Codex sessions."""

    def test_audits_explicit_codex_session_to_adjacent_output(self):
        """CLI should accept a Codex session file and produce .skill-audit.html."""
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "codex-session.jsonl"
            shutil.copy(FIXTURES / "codex-session.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report", return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            output = Path(directory) / "codex-session.skill-audit.html"
            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)
            self.assertEqual(output.read_text(encoding="utf-8"), "<html>report</html>")

    def test_default_selects_newest_across_all_sources(self):
        """CLI should consider Codex sessions when selecting the newest session."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            codex_root = base / "codex"
            codex_session = codex_root / "2026" / "10" / "07" / "rollout-newer.jsonl"
            codex_session.parent.mkdir(parents=True)
            shutil.copy(FIXTURES / "codex-session.jsonl", codex_session)
            os.utime(codex_session, (200, 200))

            kimi_root = base / "kimi"
            kimi_session = kimi_root / "workspace" / "older"
            kimi_session.mkdir(parents=True)
            kimi_wire = kimi_session / "wire.jsonl"
            kimi_wire.write_text(
                json.dumps({"timestamp": 1, "message": {"type": "TurnBegin", "payload": {
                    "user_input": [{"type": "text", "text": "older"}],
                }}}),
                encoding="utf-8",
            )
            os.utime(kimi_wire, (100, 100))

            output = base / "report.html"

            with patch.object(cli, "DEFAULT_CODEX_SESSIONS_ROOT", str(codex_root)), patch(
                "agent_session_detective.cli.render_report", return_value="<html>report</html>"
            ) as render:
                exit_code = cli.main([
                    "--sessions-root", str(kimi_root), "--out", str(output), "--no-judge",
                ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)
            self.assertEqual(output.read_text(encoding="utf-8"), "<html>report</html>")


class CodexWebTests(unittest.TestCase):
    """Test web discovery with Codex sessions."""

    def test_discovers_codex_session_file(self):
        """Web discovery should find Codex session files."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            codex_file = base / "2026" / "10" / "07" / "rollout-001.jsonl"
            codex_file.parent.mkdir(parents=True)
            codex_file.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertTrue(len(sessions) > 0)
            codex_session = next(s for s in sessions if "rollout-001" in s["id"])
            self.assertEqual(codex_session["id"], "rollout-001")
            self.assertEqual(codex_session["workspace"], "07")
            self.assertEqual(codex_session["path"], str(codex_file))

    def test_fingerprint_uses_codex_session_file(self):
        """Fingerprint should work with Codex session files."""
        with tempfile.TemporaryDirectory() as directory:
            codex_file = Path(directory) / "rollout-001.jsonl"
            codex_file.write_text("test content", encoding="utf-8")
            os.utime(codex_file, (1234, 1234))

            fp = fingerprint(str(codex_file), "test-model")
            self.assertEqual(fp, "1234.000:12:test-model:v5")


class CodexDiscoveryTests(unittest.TestCase):
    """Test find_latest_codex_session function."""

    def test_finds_newest_codex_session(self):
        """find_latest_codex_session should return the most recent session."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            older = base / "2026" / "10" / "01" / "rollout-older.jsonl"
            newer = base / "2026" / "10" / "07" / "rollout-newer.jsonl"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")
            older.touch()
            newer.touch()

            self.assertEqual(find_latest_codex_session(base), newer)


if __name__ == "__main__":
    unittest.main()
