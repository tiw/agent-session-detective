"""Billed-usage side-channel tests: uuid join, read-only query, attach."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_session_detective.billing import (
    BillingUnavailable,
    attach_billed_usage,
    query_billed_series,
    query_billed_usage,
    session_uuid_from_source,
)
from agent_session_detective.ir.schema import AuditDocument, CoverageReport

UUID = "3b241101-e2bb-4255-8caf-4136c566a962"


def make_db(path: Path, rows) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE chat_message (session_id TEXT, token_info TEXT)")
    for session_id, token_info in rows:
        conn.execute("INSERT INTO chat_message VALUES (?, ?)",
                     (session_id, token_info))
    conn.commit()
    conn.close()
    return path


def make_document() -> AuditDocument:
    return AuditDocument(
        adapter={"id": "test", "version": "0"},
        estimator_version="test",
        source_files=[],
        agents=[],
        requests=[],
        items=[],
        skills=[],
        compactions=[],
        coverage=CoverageReport(
            per_field={}, requests_with_anchor="", request_identity={},
            bucket_sources={}, unknown_channels=[], dropped_records={},
            notes=[]),
    )


class SessionUuidTest(unittest.TestCase):
    def test_uuid_shaped_transcript_stem_is_the_session_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(session_uuid_from_source(source), UUID)

    def test_uppercase_uuid_stem_normalizes_to_lower(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / (UUID.upper() + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            self.assertEqual(session_uuid_from_source(source), UUID.lower())

    def test_non_uuid_name_is_not_a_session(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tier1.jsonl"
            source.write_text("{}", encoding="utf-8")
            self.assertIsNone(session_uuid_from_source(source))

    def test_directory_source_is_not_a_session(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(session_uuid_from_source(Path(directory)))


class QueryTest(unittest.TestCase):
    def test_aggregates_token_info_rows_for_the_session_only(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80,
                                   "max_input_tokens": 180000})),
                (UUID, json.dumps({"prompt_tokens": 50, "completion_tokens": 7,
                                   "cached_tokens": 20})),
                ("other-session", json.dumps({"prompt_tokens": 999,
                                              "completion_tokens": 1,
                                              "cached_tokens": 0})),
            ])
            billed = query_billed_usage(UUID, db_path=db)
            self.assertEqual(billed.session_id, UUID)
            self.assertEqual(billed.requests, 2)
            self.assertEqual(billed.prompt_tokens, 150)
            self.assertEqual(billed.completion_tokens, 12)
            self.assertEqual(billed.cached_tokens, 100)
            self.assertEqual(billed.rows_total, 2)
            self.assertEqual(billed.rows_without_token_info, 0)
            self.assertEqual(billed.source, "SharedClientCache chat_message.token_info")
            self.assertEqual(billed.db_path, str(db))

    def test_rows_without_token_info_are_counted_never_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80})),
                (UUID, json.dumps({"model_key": "auto"})),
                (UUID, "not-json"),
            ])
            billed = query_billed_usage(UUID, db_path=db)
            self.assertEqual(billed.requests, 1)
            self.assertEqual(billed.prompt_tokens, 100)
            self.assertEqual(billed.rows_total, 3)
            self.assertEqual(billed.rows_without_token_info, 2)

    def test_a_row_missing_one_field_is_skipped_whole(self):
        # A row without parseable token_info is skipped whole: it must
        # contribute nothing — not even the field it did carry.
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80})),
                (UUID, json.dumps({"prompt_tokens": 700,
                                   "completion_tokens": 9})),
            ])
            billed = query_billed_usage(UUID, db_path=db)
            self.assertEqual(billed.requests, 1)
            self.assertEqual(billed.prompt_tokens, 100)
            self.assertEqual(billed.completion_tokens, 5)
            self.assertEqual(billed.rows_total, 2)
            self.assertEqual(billed.rows_without_token_info, 1)

    def test_zero_rows_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [])
            with self.assertRaises(BillingUnavailable) as ctx:
                query_billed_usage(UUID, db_path=db)
            self.assertIn("no billed rows", ctx.exception.reason)

    def test_all_rows_without_token_info_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_db(Path(directory) / "local.db", [
                (UUID, json.dumps({"model_key": "auto"})),
            ])
            with self.assertRaises(BillingUnavailable) as ctx:
                query_billed_usage(UUID, db_path=db)
            self.assertIn("lack parseable token_info", ctx.exception.reason)

    def test_missing_db_raises(self):
        with self.assertRaises(BillingUnavailable) as ctx:
            query_billed_usage(UUID, db_path=Path("/no/such/local.db"))
        self.assertIn("db not found", ctx.exception.reason)

    def test_missing_table_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE unrelated (x TEXT)")
            conn.commit()
            conn.close()
            with self.assertRaises(BillingUnavailable) as ctx:
                query_billed_usage(UUID, db_path=path)
            self.assertIn("chat_message", ctx.exception.reason)

    def test_corrupt_db_raises_never_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.db"
            path.write_bytes(b"this is not sqlite")
            with self.assertRaises(BillingUnavailable):
                query_billed_usage(UUID, db_path=path)


class AttachTest(unittest.TestCase):
    def test_success_sets_document_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            db = make_db(base / "local.db", [
                (UUID, json.dumps({"prompt_tokens": 100, "completion_tokens": 5,
                                   "cached_tokens": 80})),
            ])
            document = make_document()
            attach_billed_usage(document, source, db_path=db)
            self.assertIsNotNone(document.billing)
            self.assertEqual(document.billing.prompt_tokens, 100)

    def test_failure_records_a_coverage_note_and_keeps_billing_none(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / (UUID + ".jsonl")
            source.write_text("{}", encoding="utf-8")
            document = make_document()
            attach_billed_usage(document, source,
                                db_path=base / "absent.db")
            self.assertIsNone(document.billing)
            self.assertEqual(
                document.coverage.notes,
                ["billed_usage: unavailable (db not found: %s)"
                 % (base / "absent.db")])

    def test_non_qoder_source_records_the_applicability_note(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tier1.jsonl"
            source.write_text("{}", encoding="utf-8")
            document = make_document()
            attach_billed_usage(document, source)
            self.assertIsNone(document.billing)
            self.assertEqual(document.coverage.notes,
                             ["billed_usage: unavailable (not a qoder session)"])


def make_series_db(path: Path, rows) -> Path:
    """3-column chat_message (IR 1.5+): gmt_create is INTEGER epoch ms."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE chat_message "
        "(session_id TEXT, gmt_create INTEGER, token_info TEXT)")
    for gmt, info in rows:
        conn.execute("INSERT INTO chat_message VALUES (?, ?, ?)",
                     (UUID, gmt, None if info is None else json.dumps(info)))
    conn.commit()
    conn.close()
    return path


class QueryBilledSeriesTest(unittest.TestCase):
    def test_series_rows_are_ts_sorted_in_seconds(self):
        # gmt_create is INTEGER epoch milliseconds; a value below 1e11 is
        # already seconds. Rows come back time-sorted with float seconds.
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (1754709642658, {"prompt_tokens": 50000, "completion_tokens": 40,
                                 "cached_tokens": 0}),
                (1754709632658, {"prompt_tokens": 100000, "completion_tokens": 40,
                                 "cached_tokens": 80000}),
            ])
            rows = query_billed_series(UUID, db_path=db)
            self.assertEqual([r["ts"] for r in rows],
                             [1754709632.658, 1754709642.658])
            self.assertEqual(rows[0]["prompt"], 100000)
            self.assertEqual(rows[0]["cached"], 80000)

    def test_malformed_rows_are_skipped_whole(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (0, {"prompt_tokens": 1}),
                (1754709632658, None),
                (1754709642658, {"prompt_tokens": "x"}),
                (1754709652658, {"prompt_tokens": 20000, "completion_tokens": 5,
                                 "cached_tokens": 0}),
            ])
            rows = query_billed_series(UUID, db_path=db)
            self.assertEqual([r["ts"] for r in rows], [1754709652.658])

    def test_missing_db_raises_billing_unavailable(self):
        with self.assertRaises(BillingUnavailable):
            query_billed_series(UUID, db_path=Path("/nonexistent/asd-test.db"))

    def test_zero_parseable_rows_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            db = make_series_db(Path(directory) / "local.db", [
                (1754709632658, {"prompt_tokens": "x"}),
            ])
            with self.assertRaises(BillingUnavailable):
                query_billed_series(UUID, db_path=db)


if __name__ == "__main__":
    unittest.main()
