import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import cli


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


if __name__ == "__main__":
    unittest.main()
