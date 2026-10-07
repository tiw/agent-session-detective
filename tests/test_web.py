import os
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.web import discover_sessions, fingerprint


class QoderWebTests(unittest.TestCase):
    def test_discovers_qoder_transcript_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            transcript = base / "demo-workspace" / "transcript.jsonl"
            transcript.parent.mkdir()
            transcript.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(sessions, [{
                "id": "transcript",
                "path": str(transcript),
                "workspace": "demo-workspace",
                "mtime": transcript.stat().st_mtime,
            }])

    def test_discovers_qoder_transcripts_with_matching_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "project-one" / "transcript.jsonl"
            second = base / "project-two" / "transcript.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")

            sessions = discover_sessions([str(base)])

            self.assertEqual(
                {(session["id"], session["workspace"], session["path"]) for session in sessions},
                {
                    ("transcript", "project-one", str(first)),
                    ("transcript", "project-two", str(second)),
                },
            )

    def test_fingerprint_uses_v3_for_transcript_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "transcript.jsonl"
            transcript.write_text("hello", encoding="utf-8")
            os.utime(transcript, (1234, 1234))

            self.assertEqual(fingerprint(str(transcript), "test-model"), "1234.000:5:test-model:v3")


if __name__ == "__main__":
    unittest.main()
