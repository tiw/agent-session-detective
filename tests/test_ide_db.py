import base64
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_session_detective import ide_db
from agent_session_detective.wire import Event, Session, load_session

from tests.test_ir_dispatch import _clock, assistant_record, user_record

ROOT_UUID = "3b241101-e2bb-4255-8caf-4136c566a962"
KID_UUID = "1b6099f1-205a-4757-8528-d6aa9a1450e2"
KID_UUID_2 = "2c7099f1-205a-4757-8528-d6aa9a1450e3"
FIXTURES = Path(__file__).parent / "fixtures"

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


def row(row_id, role, content=None, request_id="req-1",
        token_info=None, gmt_create=1754709632658):
    return (row_id, role, content, request_id, token_info, gmt_create)


def make_db(path, children=(), messages=()):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE chat_session ("
                 "session_id TEXT PRIMARY KEY, parent_session_id TEXT, "
                 "parent_tool_call_id TEXT, session_type TEXT)")
    conn.execute("CREATE TABLE chat_message ("
                 "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                 "content TEXT, request_id TEXT, token_info TEXT, "
                 "gmt_create INTEGER)")
    for kid_id, parent_tool, session_type in children:
        conn.execute("INSERT INTO chat_session VALUES (?, ?, ?, ?)",
                     (kid_id, ROOT_UUID, parent_tool, session_type))
    for session_id, message in messages:
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (message[0], session_id, message[1], message[2],
                      message[3], message[4], message[5]))
    conn.commit()
    conn.close()


def write_main_transcript(path, tool_id="call_00A"):
    clock = _clock()
    records = [
        user_record(clock(), "m1", "kick off"),
        assistant_record(clock(), "m2",
                         tool_use=(tool_id, "Agent", {"subagent_type": "code",
                                                      "description": "spawned worker"}),
                         request_id="req-1", parent="m1"),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")


class AttachIdeDbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _transcript(self):
        path = self.base / (ROOT_UUID + ".jsonl")
        write_main_transcript(path)
        return path

    def test_non_qoder_source_is_unavailable(self):
        source = self.base / "plain.jsonl"
        session = Session(directory=self.base)
        ide_db.attach_ide_db(session, source)
        self.assertEqual(session.ide_db_stats, {
            "available": False, "reason": "not a qoder session", "db_path": ""})

    def test_missing_db_is_unavailable_with_the_path(self):
        source = self._transcript()
        session = load_session(source)
        before = len(session.events)
        missing = self.base / "absent.db"
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, missing)
        self.assertEqual(session.ide_db_stats, {
            "available": False, "reason": "db not found: %s" % missing,
            "db_path": str(missing)})
        self.assertEqual(len(session.events), before)

    def test_no_backend_is_unavailable(self):
        source = self._transcript()
        db = self.base / "local.db"
        make_db(db)
        session = load_session(source)
        with patch.object(ide_db, "_resolve_decryptor", return_value=None):
            ide_db.attach_ide_db(session, source, db)
        self.assertEqual(session.ide_db_stats, {
            "available": False,
            "reason": "no decryption backend (cryptography or openssl)",
            "db_path": str(db)})

    def test_attaches_children_with_exact_stats(self):
        source = self._transcript()
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hello", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload), "req-1",
                                         json.dumps({"prompt_tokens": 5,
                                                     "completion_tokens": 1,
                                                     "cached_tokens": 0})))])
        session = load_session(source)
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)

        self.assertEqual(session.ide_db_stats, {
            "available": True, "db_path": str(db), "children_found": 1,
            "synthesized": 1, "rows": 1, "decrypt_failures": 0,
            "skipped_already_joined": 0})
        new = session.events[before:]
        self.assertEqual([e.type for e in new], ["ContentPart", "UsageRecord"])
        for event in new:
            self.assertEqual(event.origin, "subagent:ide-db:" + KID_UUID)
            self.assertEqual(event.source,
                             source.parent / ("ide-db-%s.jsonl" % KID_UUID))
            self.assertEqual(event.ref["parent_tool_use_id"], "call_00A")

    def test_children_claimed_by_disk_records_are_skipped(self):
        source = self._transcript()
        session = load_session(source)
        session.events.append(Event(None, "ToolResult", {}, "main", source, 9,
                                    {"parent_tool_use_id": "call_00A"}))
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hello", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)

        self.assertEqual(session.ide_db_stats["skipped_already_joined"], 1)
        self.assertEqual(session.ide_db_stats["synthesized"], 0)
        self.assertEqual(len(session.events), before)

    def test_children_claimed_by_subagent_meta_are_skipped(self):
        source = self._transcript()
        session = load_session(source)
        session.subagent_meta = {"subagent:x": {"toolUseId": "call_00A"}}
        db = self.base / "local.db"
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "user",
                                         coded({"role": "user", "contents": []})))])
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        self.assertEqual(session.ide_db_stats["skipped_already_joined"], 1)

    def test_missing_tables_are_unavailable_not_a_crash(self):
        source = self._transcript()
        db = self.base / "empty.db"
        sqlite3.connect(str(db)).close()
        session = load_session(source)
        before = len(session.events)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        stats = session.ide_db_stats
        self.assertFalse(stats["available"])
        self.assertIn("chat_session", stats["reason"])
        self.assertEqual(len(session.events), before)

    def test_corrupt_db_file_never_raises(self):
        source = self._transcript()
        db = self.base / "broken.db"
        db.write_bytes(b"definitely not a sqlite db")
        session = load_session(source)
        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor):
            ide_db.attach_ide_db(session, source, db)
        self.assertFalse(session.ide_db_stats["available"])

    def test_mid_query_failure_drops_staged_events_and_stays_unavailable(self):
        source = self._transcript()
        db = self.base / "local.db"
        payload = {"role": "assistant", "content": "hi", "reasoning_content": "",
                   "tool_calls": []}
        make_db(db,
                children=[(KID_UUID, "call_00A", "agent_sub_custom"),
                          (KID_UUID_2, "call_00B", "agent_sub_custom")],
                messages=[(KID_UUID, row(1, "assistant", coded(payload)))])
        session = load_session(source)
        before = len(session.events)
        calls = {"n": 0}
        real_query_rows = ide_db._query_rows

        def flaky(conn, child_id):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.OperationalError("boom")
            return real_query_rows(conn, child_id)

        with patch.object(ide_db, "_resolve_decryptor",
                          return_value=identity_decryptor), \
                patch.object(ide_db, "_query_rows", side_effect=flaky):
            ide_db.attach_ide_db(session, source, db)

        stats = session.ide_db_stats
        self.assertFalse(stats["available"])
        self.assertIn("boom", stats["reason"])
        self.assertEqual(len(session.events), before)


if __name__ == "__main__":
    unittest.main()
