import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli
from agent_session_detective.wire import Event, Session


FIXTURES = Path(__file__).parent / "fixtures"


class QoderCliTests(unittest.TestCase):
    def test_audits_explicit_qoder_transcript_to_adjacent_output(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "foo.jsonl"
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report", return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            output = Path(directory) / "foo.skill-audit.html"
            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)
            self.assertEqual(output.read_text(encoding="utf-8"), "<html>report</html>")

    def test_terminal_cli_transcript_is_audited_degraded(self):
        records = [
            {"type": "session_meta", "sessionId": "s1", "timestamp": "2026-10-09T03:03:13Z",
             "cwd": "/w", "data": {"meta_type": "slash_command"}},
            {"type": "user", "sessionId": "s1", "timestamp": "2026-10-09T03:03:20Z",
             "cwd": "/w", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "cli-transcript.jsonl"
            transcript.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

            with patch("agent_session_detective.cli.render_report", return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)

    def test_audits_explicit_kimi_session_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "workspace" / "session"
            session.mkdir(parents=True)
            (session / "wire.jsonl").write_text(
                json.dumps({"timestamp": 1, "message": {"type": "TurnBegin", "payload": {
                    "user_input": [{"type": "text", "text": "audit this"}],
                }}}),
                encoding="utf-8",
            )

            with patch("agent_session_detective.cli.render_report", return_value="<html>report</html>"):
                exit_code = cli.main([str(session), "--no-judge"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                (session / "skill-audit.html").read_text(encoding="utf-8"), "<html>report</html>"
            )

    def test_default_selects_newest_kimi_or_qoder_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
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
            qoder_root = base / "qoder"
            transcript = qoder_root / "project" / "newer.jsonl"
            transcript.parent.mkdir(parents=True)
            shutil.copy(FIXTURES / "qoder-transcript.jsonl", transcript)
            os.utime(kimi_wire, (100, 100))
            os.utime(transcript, (200, 200))
            codex_root = base / "codex"  # empty to avoid picking up real sessions
            codex_root.mkdir(parents=True)
            output = base / "report.html"

            with patch.object(cli, "DEFAULT_QODER_PROJECTS_ROOT", str(qoder_root)), patch.object(
                cli, "DEFAULT_CODEX_SESSIONS_ROOT", str(codex_root)
            ), patch(
                "agent_session_detective.cli.render_report", return_value="<html>report</html>"
            ) as render:
                exit_code = cli.main([
                    "--sessions-root", str(kimi_root), "--out", str(output), "--no-judge",
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(render.call_args.args[0].directory, transcript.parent)
            self.assertEqual(output.read_text(encoding="utf-8"), "<html>report</html>")


class DetectAdapterTests(unittest.TestCase):
    def _session(self, *event_types, refs=None):
        refs = refs or {}
        events = [
            Event(1.0, etype, {}, "main", Path("/tmp/session.jsonl"), i + 1,
                  refs.get(i))
            for i, etype in enumerate(event_types)
        ]
        return Session(directory=Path("/tmp"), events=events)

    def test_qoder_records_carry_per_record_identity(self):
        session = self._session("TurnBegin", "ToolResult", refs={0: {"uuid": "u1"}})
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "qoder"
        )

    def test_source_format_marks_the_degraded_cli_session(self):
        session = self._session("TurnBegin", "ToolResult")
        session.source_format = "qoder-cli"
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "qoder-cli"
        )

    def test_source_format_beats_event_heuristics(self):
        session = self._session("TurnBegin", "LLMRequest")
        session.source_format = "qoder-cli"
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "qoder-cli"
        )

    def test_kimi_cli_marker_is_llm_request(self):
        session = self._session("TurnBegin", "LLMRequest")
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "kimi-cli"
        )

    def test_kimi_desktop_marker_is_turn_tokens(self):
        session = self._session("TurnBegin", "TurnTokens")
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "kimi-desktop"
        )

    def test_codex_rollout_is_recognised_by_filename(self):
        session = self._session("TurnBegin", "UsageRecord")
        source = Path("/tmp") / "rollout-2026-10-08T00-00-00-abc123.jsonl"
        self.assertEqual(cli._detect_adapter(session, source), "codex")

    def test_llm_request_marker_beats_rollout_filename(self):
        session = self._session("TurnBegin", "LLMRequest")
        source = Path("/tmp") / "rollout-2026-10-08T00-00-00-abc123.jsonl"
        self.assertEqual(cli._detect_adapter(session, source), "kimi-cli")

    def test_unrecognised_session_stays_unknown(self):
        session = self._session("TurnBegin", "UsageRecord")
        self.assertEqual(
            cli._detect_adapter(session, Path("/tmp/session.jsonl")), "unknown"
        )


class IrCliTests(unittest.TestCase):
    def run_cli(self, transcript, extra_args):
        with patch(
            "agent_session_detective.cli.render_report", return_value="<html>report</html>"
        ):
            return cli.main([str(transcript), "--no-judge"] + extra_args)

    def test_ir_out_writes_the_document_and_analyses(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            ir_path = Path(directory) / "audit.ir.json"
            analyses_path = Path(directory) / "audit.analyses.json"

            exit_code = self.run_cli(transcript, [
                "--ir-out", str(ir_path), "--ir-analyses", str(analyses_path),
            ])

            self.assertEqual(exit_code, 0)
            document = json.loads(ir_path.read_text(encoding="utf-8"))
            self.assertEqual(document["adapter"]["id"], "qoder")
            self.assertEqual(document["ir_version"], "1.5")
            self.assertEqual(
                [call["call_id"] for call in document["requests"]],
                ["main:0", "main:1", "main:2"],
            )
            analyses = json.loads(analyses_path.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(analyses),
                ["context_organization", "dispatches", "redundancy",
                 "skill_audit", "skill_loads", "skill_tree"],
            )
            self.assertEqual(
                [row["call_id"] for row in analyses["context_organization"]["rows"]],
                [call["call_id"] for call in document["requests"]],
            )

    def test_ir_analyses_without_ir_out_still_builds_the_document(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "tier1.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            analyses_path = Path(directory) / "audit.analyses.json"

            exit_code = self.run_cli(transcript, ["--ir-analyses", str(analyses_path)])

            self.assertEqual(exit_code, 0)
            analyses = json.loads(analyses_path.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(analyses),
                ["context_organization", "dispatches", "redundancy",
                 "skill_audit", "skill_loads", "skill_tree"],
            )
            self.assertEqual(
                [row["call_id"] for row in analyses["context_organization"]["rows"]],
                ["main:0", "main:1", "main:2"],
            )


BILLING_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"


class BilledUsageFlagTests(unittest.TestCase):
    def test_billed_usage_attaches_billed_totals_to_the_ir(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)
            db = base / "local.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
            conn.execute("INSERT INTO chat_message VALUES (?, ?)", (
                BILLING_UUID,
                json.dumps({"prompt_tokens": 1000, "completion_tokens": 40,
                            "cached_tokens": 800})))
            conn.commit()
            conn.close()

            exit_code = cli.main([
                str(transcript), "--no-judge", "--billed-usage",
                "--billed-db", str(db),
                "--ir-out", str(base / "ir.json"),
            ])

            self.assertEqual(exit_code, 0)
            payload = json.loads((base / "ir.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["billing"]["requests"], 1)
            self.assertEqual(payload["billing"]["prompt_tokens"], 1000)
            self.assertEqual(payload["billing"]["cached_tokens"], 800)

    def test_billed_usage_unavailable_keeps_the_audit_going(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / (BILLING_UUID + ".jsonl")
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with patch("agent_session_detective.billing.DEFAULT_DB_PATH",
                       str(base / "absent.db")), patch(
                           "agent_session_detective.cli.render_report",
                           return_value="<html>report</html>") as render:
                exit_code = cli.main(
                    [str(transcript), "--no-judge", "--billed-usage"])

            self.assertEqual(exit_code, 0)
            self.assertTrue(render.called)
            document = render.call_args.kwargs["document"]
            self.assertIsNone(document.billing)
            self.assertTrue(any(
                note.startswith("billed_usage: unavailable")
                for note in document.coverage.notes))

    def test_without_the_flag_no_attach_happens(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "foo.jsonl"
            shutil.copy(FIXTURES / "ir" / "tier1.jsonl", transcript)

            with patch("agent_session_detective.cli.render_report",
                       return_value="<html>report</html>") as render:
                exit_code = cli.main([str(transcript), "--no-judge"])

            self.assertEqual(exit_code, 0)
            document = render.call_args.kwargs["document"]
            self.assertIsNone(document.billing)
            # The tier1 fixture already emits qoder adapter notes, so the
            # opt-in contract is "no billed_usage note", not "no notes".
            self.assertFalse(any(
                note.startswith("billed_usage:")
                for note in document.coverage.notes))


if __name__ == "__main__":
    unittest.main()
