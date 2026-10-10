"""Billed-usage side-channel (opt-in): provider-billed token totals from
Qoder's local SharedClientCache SQLite DB.

Read-only and purely local — the DB is never written. Attach happens
post-build on explicit opt-in; the builder is untouched. Failures are
counted and reasoned (one coverage note), never guessed: a billed number
exists only when >=1 parseable token_info row was observed for the session
uuid.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ir.schema import AuditDocument, BilledUsage

DEFAULT_DB_PATH = Path(
    "~/Library/Application Support/Qoder/SharedClientCache/cache/db/local.db"
).expanduser()

SOURCE = "SharedClientCache chat_message.token_info"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class BillingUnavailable(Exception):
    reason: str


def session_uuid_from_source(source_path: Path) -> Optional[str]:
    """The SharedClientCache join key: the Qoder transcript's uuid stem."""
    source_path = Path(source_path)
    if not source_path.is_file() or source_path.suffix != ".jsonl":
        return None
    stem = source_path.stem
    return stem.lower() if _UUID_RE.match(stem) else None


def query_billed_usage(session_uuid: str, db_path=None) -> BilledUsage:
    path = Path(db_path or DEFAULT_DB_PATH)
    if not path.is_file():
        raise BillingUnavailable("db not found: %s" % path)
    rows_total = 0
    rows_without_token_info = 0
    prompt = completion = cached = 0
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                "SELECT token_info FROM chat_message WHERE session_id = ?",
                (session_uuid,))
            while True:
                rows = cursor.fetchmany(200)
                if not rows:
                    break
                for (raw,) in rows:
                    rows_total += 1
                    try:
                        info = json.loads(raw)
                        row = (int(info["prompt_tokens"]),
                               int(info["completion_tokens"]),
                               int(info["cached_tokens"]))
                    except (TypeError, ValueError, KeyError):
                        # Skipped whole: a row missing one field contributes
                        # none of the others, so the totals stay billed facts.
                        rows_without_token_info += 1
                    else:
                        prompt += row[0]
                        completion += row[1]
                        cached += row[2]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BillingUnavailable(str(exc))
    if rows_total == 0:
        raise BillingUnavailable(
            "no billed rows for session %s" % session_uuid)
    if rows_without_token_info == rows_total:
        raise BillingUnavailable(
            "all %d rows lack parseable token_info for session %s"
            % (rows_total, session_uuid))
    return BilledUsage(
        session_id=session_uuid,
        source=SOURCE,
        db_path=str(path),
        requests=rows_total - rows_without_token_info,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        rows_total=rows_total,
        rows_without_token_info=rows_without_token_info,
    )


def attach_billed_usage(document: AuditDocument, source_path, db_path=None) -> None:
    """Opt-in post-build attach: query the side-channel and set
    document.billing, or record one honest coverage note. Never raises."""
    uuid = session_uuid_from_source(Path(source_path))
    if uuid is None:
        document.coverage.notes.append(
            "billed_usage: unavailable (not a qoder session)")
        return
    try:
        document.billing = query_billed_usage(uuid, db_path=db_path)
    except BillingUnavailable as exc:
        document.coverage.notes.append(
            "billed_usage: unavailable (%s)" % exc.reason)
