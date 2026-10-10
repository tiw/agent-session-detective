"""Qoder IDE only: attach subagent chains from the local SharedClientCache DB.

The IDE leaves no subagent transcript on disk — child sessions live in
``chat_session`` (linked to the root by ``parent_tool_call_id``) with the
message payloads AES-encrypted in ``chat_message.content``. This module
decrypts those rows and synthesizes wire events so the existing records-path
join resolves the dispatches the disk side could not.

Opt-in and additive: off by default, never raises, DB opened read-only,
decrypted content stays in-process.
"""

import base64
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import billing
from .wire import Event, Session

_AES_KEY = b"QbgzpWzN7tfe43gf"

_CHILDREN_SQL = """
SELECT session_id, parent_tool_call_id FROM chat_session
WHERE parent_session_id = ? AND parent_tool_call_id != ''
  AND session_type LIKE 'agent_sub%'
ORDER BY session_id
"""

_ROWS_SQL = """
SELECT id, role, content, request_id, token_info, gmt_create
FROM chat_message WHERE session_id = ? ORDER BY gmt_create, id
"""


def _cryptography_decrypt(blob):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_KEY)).decryptor()
    data = decryptor.update(blob) + decryptor.finalize()
    if data:
        pad = data[-1]
        if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
            data = data[:-pad]
    return data


def _openssl_decrypt(blob):
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-cbc",
         "-K", _AES_KEY.hex(), "-iv", _AES_KEY.hex()],
        input=blob, capture_output=True)
    if proc.returncode != 0:
        raise ValueError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def _resolve_decryptor():
    try:
        from cryptography.hazmat.primitives.ciphers import (  # noqa: F401
            Cipher, algorithms, modes)
    except ImportError:
        pass
    else:
        return _cryptography_decrypt
    if shutil.which("openssl"):
        return _openssl_decrypt
    return None


def _decrypt_row(raw, decrypt):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        blob = base64.b64decode(raw)
        data = decrypt(blob)
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        payload = json.loads(data)
    except (TypeError, ValueError, OSError, ImportError):
        return None
    return payload if isinstance(payload, dict) else None


def _text_parts(decoded):
    contents = decoded.get("contents")
    if not isinstance(contents, list):
        return []
    return [{"type": "text", "text": part["text"]}
            for part in contents
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)]


def _usage_payload(decoded):
    prompt = decoded.get("prompt_tokens")
    completion = decoded.get("completion_tokens")
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    cached = decoded.get("cached_tokens")
    if not isinstance(cached, int):
        cached = 0
    return {"input_other": max(0, prompt - cached),
            "input_cache_read": cached,
            "output": completion}


def _token_usage(raw):
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return _usage_payload(payload) if isinstance(payload, dict) else None


def _synthesize_child(child_id, parent_tool_call_id, rows, decrypt, source):
    events = []
    failures = 0
    origin = "subagent:ide-db:%s" % child_id

    def emit(seq, ts, ref, event_type, payload):
        events.append(Event(ts, event_type, payload, origin, source, seq, ref))

    for seq, row in enumerate(rows, start=1):
        _row_id, role, content, request_id, token_info, gmt_create = row[:6]
        ts = billing._gmt_create_to_ts(gmt_create)
        ref = {"parent_tool_use_id": parent_tool_call_id,
               "request_id": request_id}
        decoded = _decrypt_row(content, decrypt)
        if decoded is None:
            if isinstance(content, str) and content:
                failures += 1
        elif role == "user":
            parts = _text_parts(decoded)
            if not parts:
                text = decoded.get("content")
                if isinstance(text, str) and text:
                    parts = [{"type": "text", "text": text}]
            if parts:
                emit(seq, ts, ref, "TurnBegin", {"user_input": parts})
        elif role == "assistant":
            text = decoded.get("content")
            if isinstance(text, str) and text:
                emit(seq, ts, ref, "ContentPart", {"type": "text", "text": text})
            think = decoded.get("reasoning_content")
            if isinstance(think, str) and think:
                emit(seq, ts, ref, "ContentPart",
                     {"type": "think", "think": think})
            calls = decoded.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function")
                    fn = fn if isinstance(fn, dict) else {}
                    emit(seq, ts, ref, "ToolCall", {
                        "id": call.get("id"),
                        "function": {"name": fn.get("name"),
                                     "arguments": fn.get("arguments")}})
        elif role == "tool":
            emit(seq, ts, ref, "ToolResult", {
                "tool_call_id": decoded.get("tool_call_id"),
                "return_value": {
                    "output": decoded.get("content", ""),
                    "is_error": bool(decoded.get("is_error", False)),
                },
            })
        if role == "assistant":
            usage = _token_usage(token_info)
            if usage is not None:
                emit(seq, ts, ref, "UsageRecord", usage)
    return events, failures


def _query_children(conn, parent_session_id):
    return conn.execute(_CHILDREN_SQL, (parent_session_id,)).fetchall()


def _query_rows(conn, child_id):
    return conn.execute(_ROWS_SQL, (child_id,)).fetchall()


def _claimed_tool_ids(session):
    claimed = set()
    for event in session.events:
        ref = event.ref or {}
        tool_use_id = ref.get("parent_tool_use_id")
        if tool_use_id:
            claimed.add(tool_use_id)
    for meta in session.subagent_meta.values():
        tool_use_id = meta.get("toolUseId")
        if tool_use_id:
            claimed.add(tool_use_id)
    return claimed


def _unavailable(reason, path=None):
    return {"available": False, "reason": reason,
            "db_path": str(path) if path is not None else ""}


def attach_ide_db(session, source_path, db_path=None):
    """Attach IDE DB subagent chains to ``session`` (opt-in, never raises).

    Reads direct child sessions of the root session from the SharedClientCache
    ``local.db`` and appends synthesized events to ``session.events``. On any
    failure the session is left untouched and ``session.ide_db_stats`` records
    why (unavailable). Returns None; the caller reads ``session.ide_db_stats``.
    """
    session_uuid = billing.session_uuid_from_source(source_path)
    if session_uuid is None:
        session.ide_db_stats = _unavailable("not a qoder session")
        return
    path = Path(db_path if db_path is not None else billing.DEFAULT_DB_PATH)
    if not path.is_file():
        session.ide_db_stats = _unavailable("db not found: %s" % path, path)
        return
    decrypt = _resolve_decryptor()
    if decrypt is None:
        session.ide_db_stats = _unavailable(
            "no decryption backend (cryptography or openssl)", path)
        return

    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        children = _query_children(conn, session_uuid)
        claimed = _claimed_tool_ids(session)
        staged = []
        decrypt_failures = 0
        skipped = 0
        rows_read = 0
        synthesized = 0
        for child_id, parent_tool_call_id in children:
            if parent_tool_call_id in claimed:
                skipped += 1
                continue
            child_rows = _query_rows(conn, child_id)
            source = session.directory / ("ide-db-%s.jsonl" % child_id)
            events, failures = _synthesize_child(
                child_id, parent_tool_call_id, child_rows, decrypt, source)
            decrypt_failures += failures
            rows_read += len(child_rows)
            synthesized += 1
            staged.extend(events)
    except sqlite3.Error as exc:
        session.ide_db_stats = _unavailable(str(exc), path)
        return
    finally:
        if conn is not None:
            conn.close()

    session.events.extend(staged)
    session.ide_db_stats = {
        "available": True,
        "db_path": str(path),
        "children_found": len(children),
        "synthesized": synthesized,
        "rows": rows_read,
        "decrypt_failures": decrypt_failures,
        "skipped_already_joined": skipped,
    }
