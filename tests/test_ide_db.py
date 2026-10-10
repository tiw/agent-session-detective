import base64
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import ide_db
from agent_session_detective.wire import Event

KID_UUID = "1b6099f1-205a-4757-8528-d6aa9a1450e2"

PLAINTEXT_ASSISTANT = {"role": "assistant", "content": "hello from ide-db",
                       "reasoning_content": "", "tool_calls": []}
CIPHERTEXT_ASSISTANT = (
    "sGRwjAwZdTYr1Gv7UgK2jBJTF5Qf3qjX9F365RIMIwGIn1hofSa419zqZVCDxPu1"
    "aiU5JDvdu40VraIvPuY2hpbj1GJcOD+m+5cAW1b1XSeOS/btY9J19TU0OepYCNeIk9"
    "x0Hjfse8c6RfTjv1ozDw==")


def identity_decryptor(blob):
    return blob.decode("utf-8")


def coded(payload):
    return base64.b64encode(
        json.dumps(payload).encode("utf-8")).decode("ascii")


class DecryptLadderTests(unittest.TestCase):
    def test_identity_seam_decodes_base64_json(self):
        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertEqual(
            ide_db._decrypt_row(raw, identity_decryptor), PLAINTEXT_ASSISTANT)

    def test_non_string_and_empty_rows_decrypt_to_none(self):
        self.assertIsNone(ide_db._decrypt_row(None, identity_decryptor))
        self.assertIsNone(ide_db._decrypt_row("", identity_decryptor))

    def test_bad_base64_decrypts_to_none(self):
        self.assertIsNone(ide_db._decrypt_row("abcde", identity_decryptor))

    def test_bad_json_decrypts_to_none(self):
        raw = base64.b64encode(b"not json").decode("ascii")
        self.assertIsNone(ide_db._decrypt_row(raw, identity_decryptor))

    def test_non_dict_json_decrypts_to_none(self):
        raw = base64.b64encode(b"[1, 2]").decode("ascii")
        self.assertIsNone(ide_db._decrypt_row(raw, identity_decryptor))

    def test_decryptor_raising_value_error_decrypts_to_none(self):
        def boom(blob):
            raise ValueError("bad padding")

        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertIsNone(ide_db._decrypt_row(raw, boom))

    def test_decryptor_raising_oserror_decrypts_to_none(self):
        def boom(blob):
            raise FileNotFoundError("openssl vanished")

        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertIsNone(ide_db._decrypt_row(raw, boom))

    def test_decryptor_raising_import_error_decrypts_to_none(self):
        def boom(blob):
            raise ImportError("broken cryptography install")

        raw = coded(PLAINTEXT_ASSISTANT)
        self.assertIsNone(ide_db._decrypt_row(raw, boom))

    def test_real_ciphertext_decrypts_with_the_resolved_backend(self):
        decrypt = ide_db._resolve_decryptor()
        if decrypt is None:
            self.skipTest("no decryption backend available")
        self.assertEqual(
            ide_db._decrypt_row(CIPHERTEXT_ASSISTANT, decrypt),
            PLAINTEXT_ASSISTANT)


class SynthesizeChildTests(unittest.TestCase):
    def _rows(self, *specs):
        return [tuple(spec) for spec in specs]

    def test_assistant_row_emits_parts_tools_and_usage_in_order(self):
        decoded = {"role": "assistant", "content": "working on it",
                   "reasoning_content": "let me think",
                   "tool_calls": [{"id": "call_1", "type": "function",
                                   "function": {"name": "Read",
                                                "arguments": "{\"file_path\": \"/a\"}"}}]}
        rows = self._rows((1, "assistant", coded(decoded), "req-1",
                           json.dumps({"prompt_tokens": 100,
                                       "completion_tokens": 7,
                                       "cached_tokens": 40}), 1754709632658))
        source = Path("/tmp/ide-db-%s.jsonl" % KID_UUID)
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor, source)

        self.assertEqual(failures, 0)
        self.assertEqual([e.type for e in events], [
            "ContentPart", "ContentPart", "ToolCall", "UsageRecord"])
        text, think, call, usage = events
        self.assertEqual(text.payload, {"type": "text", "text": "working on it"})
        self.assertEqual(think.payload, {"type": "think", "think": "let me think"})
        self.assertEqual(call.payload["id"], "call_1")
        self.assertEqual(call.payload["function"]["name"], "Read")
        self.assertEqual(call.payload["function"]["arguments"],
                         "{\"file_path\": \"/a\"}")
        self.assertEqual(usage.payload, {"input_other": 60,
                                         "input_cache_read": 40, "output": 7})
        for event in events:
            self.assertEqual(event.origin, "subagent:ide-db:" + KID_UUID)
            self.assertEqual(event.source, source)
            self.assertEqual(event.seq, 1)
            self.assertEqual(event.ts, 1754709632.658)
            self.assertEqual(event.ref, {"parent_tool_use_id": "call_00A",
                                         "request_id": "req-1"})

    def test_user_row_emits_turn_begin_from_contents(self):
        decoded = {"role": "user",
                   "contents": [{"type": "text", "text": "do the thing"},
                                {"type": "image", "data": "x"}]}
        rows = [(1, "user", coded(decoded), "req-2", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(failures, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "TurnBegin")
        self.assertEqual(events[0].payload["user_input"],
                         [{"type": "text", "text": "do the thing"}])

    def test_user_row_with_plaintext_content_falls_back(self):
        decoded = {"role": "user", "content": "brief without contents"}
        rows = [(1, "user", coded(decoded), "req-2", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(failures, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "TurnBegin")
        self.assertEqual(events[0].payload["user_input"],
                         [{"type": "text", "text": "brief without contents"}])

    def test_user_row_with_no_text_parts_emits_nothing(self):
        decoded = {"role": "user", "contents": [{"type": "image", "data": "x"}]}
        rows = [(1, "user", coded(decoded), "req-2", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual((events, failures), ([], 0))

    def test_tool_row_emits_a_tool_result(self):
        decoded = {"role": "tool", "content": "file contents",
                   "name": "Read", "tool_call_id": "call_1"}
        rows = [(1, "tool", coded(decoded), "req-3", None, 1754709632658)]
        events, _ = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(events[0].type, "ToolResult")
        self.assertEqual(events[0].payload, {
            "tool_call_id": "call_1",
            "return_value": {"output": "file contents", "is_error": False}})

    def test_undecryptable_assistant_row_still_emits_plaintext_usage(self):
        rows = [(1, "assistant", "abcde", "req-1",
                 json.dumps({"prompt_tokens": 10, "completion_tokens": 2,
                             "cached_tokens": 0}), 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual(failures, 1)
        self.assertEqual([e.type for e in events], ["UsageRecord"])
        self.assertEqual(events[0].payload, {"input_other": 10,
                                             "input_cache_read": 0, "output": 2})

    def test_empty_content_is_not_counted_as_a_failure(self):
        rows = [(1, "assistant", "", "req-1", None, 1754709632658)]
        events, failures = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual((events, failures), ([], 0))

    def test_seq_follows_row_order(self):
        decoded = {"role": "user", "contents": [{"type": "text", "text": "hi"}]}
        rows = [(1, "user", coded(decoded), "req-1", None, 1754709632658),
                (2, "user", coded(decoded), "req-2", None, 1754709632659)]
        events, _ = ide_db._synthesize_child(
            KID_UUID, "call_00A", rows, identity_decryptor,
            Path("/tmp/ide-db.jsonl"))
        self.assertEqual([e.seq for e in events], [1, 2])
        self.assertEqual([e.ts for e in events], [1754709632.658, 1754709632.659])


if __name__ == "__main__":
    unittest.main()
