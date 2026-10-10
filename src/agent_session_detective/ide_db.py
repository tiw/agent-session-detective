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
        import cryptography  # noqa: F401
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
    except (TypeError, ValueError):
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
