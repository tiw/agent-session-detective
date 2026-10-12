"""Local web application: browse sessions, run audits, read reports.

Stdlib only. JSON API served by http.server; the UI is a single-page app
under webapp/. Audit jobs run on a background thread and are polled by id.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from .bill import build_bill
from .catalog import (
    load_catalog,
    load_injected,
    load_skill_dir,
    merge_catalog,
    parse_failures,
    parse_injected,
)
from .catalog_audit import (
    DEFAULT_LARGE_LINES,
    DEFAULT_MAX_DESC_CHARS,
    DEFAULT_MAX_LINES,
    DEFAULT_MIN_DESC_CHARS,
    audit_catalog,
)
from .if_eval import IFResult, evaluate_playbook
from .ir.schema import IR_VERSION
from .judge import Judge, Judgment, judge_session
from .timeline import Timeline, build_timeline
from .tokenstats import (
    BUCKET_KEYS,
    TokenStats,
    build_token_stats,
    merge_compaction_windows,
)
from .wire import Session, find_latest_session, load_session

WEBAPP_DIR = Path(__file__).parent / "webapp"

DEFAULT_ROOTS = ["~/.kimi-code/sessions", "~/.kimi/sessions", "~/.qoder/projects", "~/.codex/sessions"]

CACHE_DIR = Path("~/.cache/agent-session-detective").expanduser()

UPLOADS_DIR = Path("~/.local/share/agent-session-detective/uploads").expanduser()
MAX_UPLOAD_BYTES = 1 << 30  # 1 GiB


class UploadRejected(Exception):
    """The payload is not an analyzable session file (bad format, no events)."""


# --------------------------------------------------------------------------
# audit result cache: same log fingerprint + same params + same judge model
# -> same result is served from disk instead of recomputing. Judge verdicts
# carry LLM variance even at temperature 0, so the UI marks cached results.
# --------------------------------------------------------------------------

def cache_key(params: dict) -> str:
    raw = json.dumps(
        {
            "path": str(Path(params["path"]).expanduser()),
            "expect": sorted(params.get("expect") or []),
            "steps": sorted(params.get("steps") or []),
            "judge_triggers": bool(params.get("judge_triggers")),
            "billed": bool(params.get("billed")),
            # The catalog is part of the result, so two runs that differ only
            # in which directories were scanned must not share a cache entry.
            "skills_dirs": sorted(
                str(Path(d).expanduser()) for d in params.get("skills_dirs") or []),
            # Injected text becomes catalog content, so two runs differing
            # only in what the auditor supplied must not share a cache entry.
            "injected": _injected_fingerprint(params),
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint(session_path: str, judge_model: str) -> str:
    """Log identity: source log metadata plus judge model.

    Directory inputs include every wire file; a transcript-file input uses
    that file's mtime and size directly.
    """
    base = Path(session_path).expanduser()
    if base.is_file():
        wires = [base]
    else:
        wires = [base / "wire.jsonl"]
        sub = base / "subagents"
        agents = base / "agents"
        if sub.is_dir():
            wires += list(sub.glob("*/wire.jsonl"))
        if agents.is_dir():
            wires += list(agents.glob("*/wire.jsonl"))
    newest, total = 0.0, 0
    for w in wires:
        try:
            st = w.stat()
            newest = max(newest, st.st_mtime)
            total += st.st_size
        except OSError:
            continue
    # RESULT_VERSION bumps whenever the result payload shape changes (e.g.
    # turn items added): old cached results would render with missing data.
    # v3 adds timeline.compaction_count.
    # v4 reconstructs per-turn context from billing records, so cached
    # results for billing-only logs (Qoder/Codex) lack turn_growth.
    # v5 counts main-agent compactions only (subagent compaction windows
    # no longer inflate compaction_count or mark turns compaction-crossed).
    # v6 adds the IR 1.1 evidence-backed skill_loads block and drops the
    # lifecycle cost claim — v5 cached results would render without it.
    # v7 canonicalizes merged skill ids in the skill_loads ledger (IR 1.2),
    # so v6 cached results would carry pre-merge rows.
    # v8 appends ir<IR_VERSION> to the key: hand bumps for IR-driven payload
    # changes (v6, v7) had missed IR 1.3-1.6, so stale cached results kept
    # being served. From here every IR bump self-invalidates; the counter
    # stays for web-local payload changes.
    # v9 adds repeat-injection classification (per-occurrence classes,
    # repeat_class_totals, compaction_source/points) — v8 cached results
    # would render repeats without the class badges and the split caption.
    # v10 feeds the billed prompt series into turn_growth: v9 cached results
    # for billed CLI-shape logs (no transcript telemetry) lack the growth
    # rows and would keep rendering the "no context telemetry" gap note.
    # v11 adds the bill + subagent_returns payload blocks (P0-1): v10
    # cached results would render without the bill card.
    return "%.3f:%d:%s:v11:ir%s" % (newest, total, judge_model, IR_VERSION)


def cache_load(key: str, fp: str) -> Optional[dict]:
    path = CACHE_DIR / (key + ".json")
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if entry.get("fingerprint") != fp:
        return None
    return entry.get("result")


def cache_store(key: str, fp: str, result: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"fingerprint": fp, "result": result}
        (CACHE_DIR / (key + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def cached_paths() -> set:
    """Session paths that have at least one cached audit (for sidebar dots)."""
    out = set()
    if not CACHE_DIR.is_dir():
        return out
    for f in CACHE_DIR.glob("*.json"):
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            out.add(entry.get("result", {}).get("session", {}).get("path", ""))
        except (OSError, json.JSONDecodeError):
            continue
    out.discard("")
    return out


# --------------------------------------------------------------------------
# uploaded sessions: user-provided transcript files, persisted under
# UPLOADS_DIR and analyzed exactly like scanned sessions (the whole audit
# flow keys off the file path).
# --------------------------------------------------------------------------

def _manifest_path() -> Path:
    return UPLOADS_DIR / "uploads.json"


def load_upload_manifest() -> Dict[str, dict]:
    try:
        manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _save_manifest(manifest: Dict[str, dict]) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _manifest_path().write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def _sanitize_upload_name(name: str) -> str:
    """A filesystem-safe stem from the original filename. Path separators
    and control characters are stripped so an upload name never escapes
    the uploads directory."""
    stem = Path(name.replace("\\", "/")).name
    stem = "".join(ch for ch in stem if ch.isprintable())
    stem = stem.rsplit(".", 1)[0].strip(" .") or "upload"
    return stem[:120]


def _upload_entry(stored: str) -> dict:
    path = UPLOADS_DIR / stored
    return {
        "id": path.stem,
        "path": str(path),
        "workspace": "upload",
        "mtime": path.stat().st_mtime,
        "uploaded": True,
    }


def store_upload(body: bytes, original_name: str) -> dict:
    """Persist one uploaded session file; returns the discovery-shaped
    session entry plus ``dedup`` and ``bytes``.

    The payload must parse through the same ``load_session`` the audit
    flow uses — a file that yields nothing analyzable is rejected rather
    than stored as a dead sidebar row. Same content (sha1) re-uploads map
    to the existing file instead of a duplicate row.
    """
    digest = hashlib.sha1(body).hexdigest()
    manifest = load_upload_manifest()
    for stored, entry in manifest.items():
        if entry.get("sha1") == digest and (UPLOADS_DIR / stored).is_file():
            return {"session": _upload_entry(stored), "dedup": True,
                    "bytes": len(body)}
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.jsonl"
        probe.write_bytes(body)
        try:
            session = load_session(probe)
            # Zero events is dead; a file whose every record was dropped
            # (malformed json, non-dict) is just as dead for the sidebar —
            # only RecordDropped events means nothing analyzable survived.
            if not session.events or all(
                    e.type == "RecordDropped" for e in session.events):
                raise ValueError("no events parsed")
        except Exception as exc:
            raise UploadRejected(str(exc)) from exc
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    stem = _sanitize_upload_name(original_name)
    stored = stem + ".jsonl"
    n = 2
    while (UPLOADS_DIR / stored).exists():
        stored = "%s-%d.jsonl" % (stem, n)
        n += 1
    (UPLOADS_DIR / stored).write_bytes(body)
    manifest[stored] = {
        "original": Path(original_name.replace("\\", "/")).name,
        "sha1": digest,
        "uploaded_at": time.time(),
        "size": len(body),
    }
    _save_manifest(manifest)
    return {"session": _upload_entry(stored), "dedup": False, "bytes": len(body)}


def delete_upload(path_str: str) -> Optional[str]:
    """Remove one uploaded session file. Returns the stored name, or None
    when the path is not an uploaded file (outside the uploads directory,
    wrong suffix, or absent) — a DELETE can never touch scanned sessions."""
    base = UPLOADS_DIR.resolve()
    try:
        target = Path(path_str).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if target.parent != base or target.suffix != ".jsonl" or not target.is_file():
        return None
    stored = target.name
    target.unlink(missing_ok=True)
    manifest = load_upload_manifest()
    if manifest.pop(stored, None) is not None:
        _save_manifest(manifest)
    return stored


# --------------------------------------------------------------------------
# serialization: dataclasses -> JSON-safe dicts (ReportData shape)
# --------------------------------------------------------------------------

def timeline_to_dict(timeline: Timeline) -> dict:
    return {
        "loads": [
            {
                "skill_name": l.skill_name,
                "ts": l.ts,
                "origin": l.origin,
                "is_error": l.is_error,
                "tokens_est": l.tokens_est,
                "context_tokens_after": l.context_tokens_after,
                "evicted_by": l.evicted_by,
                "content_head": l.content[:600],
                "source_line": l.source_line,
            }
            for l in timeline.loads
        ],
        "file_reads": [
            {
                "path": r.path,
                "skill_name": r.skill_name,
                "ts": r.ts,
                "origin": r.origin,
                "snippet_head": r.snippet[:300],
            }
            for r in timeline.file_reads
        ],
        "compactions": [
            {"index": c.index, "begin_ts": c.begin_ts, "end_ts": c.end_ts}
            for c in timeline.compactions
        ],
        "compaction_count": (
            len(timeline.compactions) if timeline.compaction_telemetry_available else None
        ),
        "status_series": [
            {"ts": e.ts, "context_tokens": e.payload.get("context_tokens")}
            for e in timeline.status_series
        ],
        "turns": [
            {"ts": e.ts, "text": e.text_preview(limit=400)} for e in timeline.turns
        ],
    }


def tokenstats_to_dict(s: TokenStats) -> dict:
    return {
        "cache_hit_rate": s.cache_hit_rate,
        "input_total": s.input_total,
        "output_total": s.output_total,
        "cache_read_total": s.cache_read_total,
        "growth_verdict": s.growth_verdict,
        "turn_growth": [
            {
                "turn": r.turn,
                "context_at_start": r.context_at_start,
                "exact": r.exact,
                "added": r.added,
                **{key + "_added": getattr(r, key + "_added") for key in BUCKET_KEYS},
                "crossed_compaction": r.crossed_compaction,
                # drill-down: biggest contributors first, capped; previews only
                "items": [
                    {"bucket": it.bucket, "tokens": it.tokens, "preview": it.preview}
                    for it in sorted(r.items, key=lambda x: -x.tokens)[:40]
                    if it.tokens >= 5
                ],
            }
            for r in s.turn_growth
        ],
        "bucket_totals": s.bucket_totals,
        "bucket_shares": s.bucket_shares,
        "hash_runs": [
            {
                "kind": r.kind,
                "hash": r.hash[:12],
                "first_ts": r.first_ts,
                "last_ts": r.last_ts,
                "requests": r.requests,
            }
            for r in s.hash_runs
        ],
        "hash_flips": s.hash_flips,
        "repeats": [
            {
                "preview": r.preview,
                "occurrences": r.occurrences,
                "tokens_each": r.tokens_each,
                "extra_tokens": r.extra_tokens,
                "tool_name": r.tool_name,
                "chars_each": r.chars_each,
                "occurrence_classes": r.occurrence_classes,
                "extra_by_class": r.extra_by_class,
            }
            for r in s.repeats
        ],
        "repeat_extra_tokens": s.repeat_extra_tokens,
        "repeat_class_totals": s.repeat_class_totals,
        "compaction_source": s.compaction_source,
        "compaction_points": s.compaction_points,
        "usage_record_count": len(s.usage_records),
    }


def judgments_to_dict(judgments: List[Judgment]) -> dict:
    return {
        "missed": [
            {
                "skill_name": j.skill_name,
                "turn": j.turn,
                "rationale": j.rationale,
                "evidence": j.evidence,
                "confidence": j.confidence,
            }
            for j in judgments if j.triggered
        ],
        "errors": sorted({j.error or "" for j in judgments if j.error}),
    }


def if_results_to_dict(results: List[IFResult]) -> List[dict]:
    return [
        {
            "playbook": r.playbook,
            "coverage": round(r.coverage, 3),
            "gate": r.gate,
            "passed": r.passed,
            "not_applicable": r.not_applicable,
            "verdicts": [
                {
                    "step": v.step,
                    "status": v.status,
                    "evidence": v.evidence,
                    "rationale": v.rationale,
                }
                for v in r.verdicts
            ],
        }
        for r in results
    ]


def event_kind(event) -> str:
    """Classify an event by who produced it, for role-distinguished feeds."""
    if event.type == "TurnBegin":
        return "user"
    if event.type == "ContentPart":
        return "think" if event.payload.get("type") == "think" else "say"
    if event.type == "ToolCall":
        return "tool"
    if event.type == "ToolResult":
        return "result"
    if event.type.startswith("Compaction"):
        return "compact"
    if event.type == "StatusUpdate":
        return "status"
    return "sys"


def event_feed_to_list(session: Session, limit: int = 400) -> List[dict]:
    feed = []
    for e in session.events[-limit:]:
        feed.append(
            {
                "ts": e.ts,
                "type": e.type,
                "kind": event_kind(e),
                "origin": e.origin,
                "text": e.text_preview(limit=160),
            }
        )
    return feed


# --------------------------------------------------------------------------
# session discovery
# --------------------------------------------------------------------------

def discover_sessions(roots: Optional[List[str]] = None) -> List[dict]:
    """Stat-level discovery only: no log parsing. Cheap enough to run on
    every page load; per-session stats are computed on demand when an
    audit job actually runs."""
    roots = roots or DEFAULT_ROOTS
    found: Dict[str, dict] = {}
    for root in roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            continue
        wires = list(base.glob("*/*/agents/main/wire.jsonl")) + list(
            base.glob("*/*/wire.jsonl")
        )
        for wire in wires:
            # agents layout: <root>/<ws>/<session>/agents/main/wire.jsonl
            # cli layout:    <root>/<ws>/<session>/wire.jsonl
            session_dir = wire.parents[2] if wire.parent.name == "main" else wire.parent
            sid = session_dir.name
            try:
                mtime = wire.stat().st_mtime
            except OSError:
                continue
            if sid in found and found[sid]["mtime"] >= mtime:
                continue
            found[sid] = {
                "id": sid,
                "path": str(session_dir),
                "workspace": session_dir.parent.name,
                "mtime": mtime,
            }
        for transcript in base.glob("*/*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            sid = transcript.stem
            source_key = "qoder:%s" % transcript.resolve()
            if source_key in found and found[source_key]["mtime"] >= mtime:
                continue
            found[source_key] = {
                "id": sid,
                "path": str(transcript),
                "workspace": transcript.parent.name,
                "mtime": mtime,
            }
        # Qoder terminal CLI transcripts: <root>/<ws>/transcript/<uuid>.jsonl.
        # They load degraded (no usage telemetry) but belong in the sidebar;
        # the workspace shown is the real workspace dir above transcript/.
        for transcript in base.glob("*/transcript/*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            sid = transcript.stem
            source_key = "qoder-cli:%s" % transcript.resolve()
            if source_key in found and found[source_key]["mtime"] >= mtime:
                continue
            found[source_key] = {
                "id": sid,
                "path": str(transcript),
                "workspace": transcript.parents[1].name,
                "mtime": mtime,
            }
        # Codex sessions: <root>/YYYY/MM/DD/rollout-*.jsonl. The rollout-
        # prefix matters: this glob also runs against the qoder projects
        # root, where depth-4 jsonl files exist but are subagent mirrors
        # inside <ws>/<session>/subagents/ — those must not surface as
        # codex sessions.
        for codex_file in base.glob("*/*/*/rollout-*.jsonl"):
            try:
                mtime = codex_file.stat().st_mtime
            except OSError:
                continue
            sid = codex_file.stem
            source_key = "codex:%s" % codex_file.resolve()
            if source_key in found and found[source_key]["mtime"] >= mtime:
                continue
            found[source_key] = {
                "id": sid,
                "path": str(codex_file),
                "workspace": codex_file.parent.name,  # date folder
                "mtime": mtime,
            }
    # Uploaded sessions: user-provided files persisted under UPLOADS_DIR.
    # Same listing shape as scanned sessions; the flag drives the upload
    # badge and the row delete button in the UI.
    if UPLOADS_DIR.is_dir():
        for transcript in UPLOADS_DIR.glob("*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            source_key = "upload:%s" % transcript.resolve()
            if source_key in found and found[source_key]["mtime"] >= mtime:
                continue
            found[source_key] = {
                "id": transcript.stem,
                "path": str(transcript),
                "workspace": "upload",
                "mtime": mtime,
                "uploaded": True,
            }
    return sorted(found.values(), key=lambda s: -s["mtime"])


SIDEBAR_LIMIT = 10
SEARCH_LIMIT = 50


def list_sessions(
    roots: Optional[List[str]] = None, query: Optional[str] = None
) -> dict:
    """The most recent sessions for the sidebar. Parsing a session's log is
    deferred to the audit job; this endpoint must stay instant.

    ``query`` searches the full discovery result by session id or workspace,
    because discovery finds far more sessions than the sidebar shows."""
    audited = cached_paths()
    all_sessions = discover_sessions(roots)
    needle = (query or "").strip().lower()
    if needle:
        matches = [
            s for s in all_sessions
            if needle in s["id"].lower() or needle in s["workspace"].lower()
        ]
        sessions = matches[:SEARCH_LIMIT]
    else:
        sessions = all_sessions[:SIDEBAR_LIMIT]
    for s in sessions:
        s["cached"] = s["path"] in audited
    return {"sessions": sessions, "total": len(all_sessions), "fleet": None}


def fleet_stats(roots: Optional[List[str]] = None) -> Dict[str, object]:
    """Cross-session aggregation, on demand: parses every discovered
    session's log, so the caller should treat this as a slow operation
    (the UI shows progress while it runs)."""
    audited = cached_paths()
    fleet: Dict[str, object] = {
        "sessions": 0,
        "with_usage": 0,
        "avg_turns": None,
        "avg_cache_hit_rate": None,
        "growth_shapes": {},
        "total_output_tokens": 0,
        "total_repeat_extra_tokens": 0,
        "repeat_class_totals": {},
    }
    turns_sum = 0
    hit_sum = 0.0
    for s in discover_sessions(roots):
        s["cached"] = s["path"] in audited
        try:
            session = load_session(Path(s["path"]))
            timeline = build_timeline(session)
            s["turns"] = len(timeline.turns)
            s["events"] = len(session.events)
            s["skills_loaded"] = len(timeline.loads)
            s["skill_files_read"] = len(timeline.file_reads)
            windows, source = merge_compaction_windows(timeline)
            stats = build_token_stats(
                session, timeline,
                compaction_windows=windows, compaction_source=source)
            s["cache_hit_rate"] = stats.cache_hit_rate
            s["growth_verdict"] = stats.growth_verdict
            s["repeat_extra_tokens"] = stats.repeat_extra_tokens
            s["repeat_class_totals"] = stats.repeat_class_totals
            s["output_total"] = stats.output_total
        except Exception:
            s["turns"] = s["events"] = s["skills_loaded"] = s["skill_files_read"] = None
            s["cache_hit_rate"] = s["growth_verdict"] = None
            s["repeat_extra_tokens"] = s["output_total"] = None
            s["repeat_class_totals"] = None
        if s["turns"] is None:
            continue
        fleet["sessions"] = fleet["sessions"] + 1
        turns_sum += s["turns"]
        shape = (s["growth_verdict"] or "unknown").split(" ")[0]
        fleet["growth_shapes"][shape] = fleet["growth_shapes"].get(shape, 0) + 1
        if s["cache_hit_rate"] is not None:
            fleet["with_usage"] = fleet["with_usage"] + 1
            hit_sum += s["cache_hit_rate"]
        fleet["total_output_tokens"] += s["output_total"] or 0
        fleet["total_repeat_extra_tokens"] += s["repeat_extra_tokens"] or 0
        for cls, extra in (s["repeat_class_totals"] or {}).items():
            fleet["repeat_class_totals"][cls] = \
                fleet["repeat_class_totals"].get(cls, 0) + extra
    if fleet["sessions"]:
        fleet["avg_turns"] = round(turns_sum / fleet["sessions"], 1)
    if fleet["with_usage"]:
        fleet["avg_cache_hit_rate"] = round(hit_sum / fleet["with_usage"], 3)
    return fleet


# --------------------------------------------------------------------------
# audit jobs
# --------------------------------------------------------------------------

class Job:
    def __init__(self, params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.params = params
        self.status = "queued"
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.steps: List[str] = []
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    def mark(self, text: str) -> None:
        self.steps.append(text)


def suggest_next_steps(
    timeline: Timeline,
    expectations: List[dict],
    judgments: List[Judgment],
    if_results: List[IFResult],
    judge_enabled: bool,
    token_stats: Optional[TokenStats] = None,
) -> List[str]:
    """1-3 derived next actions, per the delivery checklist: summary plus
    suggested follow-ups, not just a wall of results."""
    out: List[str] = []
    missing = [e["name"] for e in expectations if e["status"] == "missing"]
    if missing:
        out.append("检查缺失 skill 是否安装或注册: %s" % ", ".join(missing[:3]))
    failed_if = [r for r in if_results if not r.passed and not r.not_applicable]
    if failed_if:
        out.append(
            "IF 未过门槛 (%s): 展开 skipped/partial 步骤核对是裁量还是遗漏"
            % ", ".join("%.2f" % r.coverage for r in failed_if)
        )
    defects = [j for j in judgments if j.error]
    if defects:
        out.append("%d 条判定被丢弃为工具缺陷: 先怀疑解析器，别急着怀疑 agent" % len(defects))
    if len(timeline.file_reads) >= 3:
        out.append(
            "%d 次 SKILL.md 直读: 确认该 harness 下是否为正规加载方式（点板块旁的 ? 查看语义）"
            % len(timeline.file_reads)
        )
    if token_stats is not None:
        if token_stats.cache_hit_rate is not None and token_stats.cache_hit_rate < 0.5:
            out.append(
                "缓存命中率仅 %.0f%%: 检查常驻内容是否稳定排在消息最前，对齐 provider 前缀缓存"
                % (token_stats.cache_hit_rate * 100)
            )
        if token_stats.hash_flips:
            out.append(
                "system prompt/tools 哈希中途翻转 %d 次: 每次翻转都使 provider 前缀缓存失效、重付 prefill"
                % token_stats.hash_flips
            )
        if token_stats.repeat_extra_tokens >= 5000:
            post = (token_stats.repeat_class_totals or {}).get(
                "post_compaction", 0)
            tax = token_stats.repeat_extra_tokens - post
            if tax < 5000 and token_stats.compaction_source:
                out.append(
                    "重复注入 %d token 属压缩恢复协议内合法重读: 真实重读税仅 %d"
                    % (token_stats.repeat_extra_tokens, tax)
                )
            else:
                out.append(
                    "重复注入约 %d token: 同一份工具结果被多次读入，考虑状态外置 + 派生摘要"
                    % token_stats.repeat_extra_tokens
                )
        if token_stats.growth_verdict.startswith("accelerat"):
            out.append("每轮 token 增量在加速: 优先落地边界合同（派发传指针、回收收签收单）")
    if not timeline.loads and not timeline.file_reads:
        out.append("未检测到任何 skill 消费: 可能是本会话确实没用 skill，也可能是解析器缺口")
    if not judge_enabled:
        out.append("missed-trigger 判定未启用: 配置 ASD_JUDGE_API_KEY / ASD_JUDGE_MODEL 后重跑")
    if not out:
        out.append("在已配置的判定范围内未见异常: 扩大 --expect 清单或开启 judge 覆盖更多路由")
    return out[:3]


JOBS: Dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _ir_adapter_id(session, source_path) -> str:
    """Best-effort adapter id for the IR document (same fingerprints as cli)."""
    if getattr(session, "source_format", None) == "qoder-cli":
        return "qoder-cli"
    if any(event.ref is not None for event in session.events):
        return "qoder"
    kinds = {event.type for event in session.events}
    if "LLMRequest" in kinds:
        return "kimi-cli"
    if kinds & {"TurnTokens", "SubagentSpawned"}:
        return "kimi-desktop"
    if source_path.name.startswith("rollout-"):
        return "codex"
    return "unknown"


def render_tree_page(session_path: str, billed: bool = False, ide_db: bool = False) -> str:
    """The actual skill tree for one session, as a standalone HTML page.

    Same IR projection the audit job uses, minus judge and catalog: the
    tree derives from the document alone, so no audit cache and no
    background job are involved. ``billed=True`` opt-in attaches the
    provider-billed side-channel before rendering (no db-path over HTTP).
    """
    from .ir.builder import build_audit_document
    from .tree_html import render_skill_tree

    source_path = Path(session_path).expanduser()
    session = load_session(source_path)
    if not session.events:
        raise ValueError("no events parsed")
    if ide_db:
        from .ide_db import attach_ide_db
        attach_ide_db(session, source_path)
    document = build_audit_document(session, _ir_adapter_id(session, source_path))
    if billed:
        from .billing import attach_billed_usage
        attach_billed_usage(document, source_path)
    hint = None
    if not ide_db:
        from .ide_db import suggest_ide_db_hint
        hint = suggest_ide_db_hint(session, source_path, document)
    return render_skill_tree(document, ide_db_hint=hint)


def scan_skill_dirs(dirs: List[str]) -> dict:
    """Report what each candidate directory contributes to the catalog.

    Lets the audit form show what a directory holds before it is committed to
    a run — a typo'd path is otherwise indistinguishable from a directory that
    simply has no skills in it.
    """
    entries = []
    total = 0
    for raw in dirs:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        item = {"path": str(path), "exists": path.is_dir(), "skills": 0, "parse_failures": 0}
        if path.is_dir():
            # Preview exactly what the run will load: supplied directories get
            # the deep walk, so a nested plugin cache reports its real count
            # here rather than an alarming zero.
            skills = load_skill_dir(path, recursive=True)
            item["skills"] = len(skills)
            item["parse_failures"] = len(parse_failures(skills))
            item["names"] = [s.name for s in skills[:20]]
            total += item["skills"]
        entries.append(item)
    return {"dirs": entries, "skills": total}


def run_catalog_audit(params: dict) -> dict:
    """Static catalog check for the web form.

    Same measurement the ``asd catalog-audit`` command makes, so the two can
    never disagree. Takes the same directory and injection inputs as a normal
    audit — the form collects them once — but needs no session log, no judge,
    and no credentials.
    """
    dirs = params.get("skills_dirs") or []
    skill_dirs = [Path(d).expanduser() for d in dirs if isinstance(d, str) and d.strip()]
    catalog = load_catalog(extra_dirs=skill_dirs)
    injected = _injected_for(params)
    if injected:
        catalog = merge_catalog(catalog, injected)

    def _threshold(name: str, default: int, floor: int) -> int:
        raw = params.get(name)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        # A threshold below the floor would grade everything as a defect and
        # read as a real finding, so an out-of-range value falls back.
        return value if value >= floor else default

    audit = audit_catalog(
        catalog,
        max_lines=_threshold("max_lines", DEFAULT_MAX_LINES, 1),
        large_lines=_threshold("large_lines", DEFAULT_LARGE_LINES, 0),
        min_desc_chars=_threshold("min_desc_chars", DEFAULT_MIN_DESC_CHARS, 0),
        max_desc_chars=_threshold("max_desc_chars", DEFAULT_MAX_DESC_CHARS, 1),
    )
    payload = audit.to_dict()
    payload["skills_dirs"] = [str(d) for d in skill_dirs]
    payload["injected_count"] = len(injected)
    payload["top"] = [
        e.to_dict() for e in audit.size[:20]
    ]
    payload["desc_top"] = [
        e.to_dict() for e in audit.descriptions if e.tier != "ok"
    ][:20]
    return payload


def _injected_fingerprint(params: dict) -> str:
    """Identity of the supplied skill text, for cache separation.

    Built from the resolved entries rather than the raw string so cosmetic
    reformatting of the same JSON does not orphan a cached result, while a
    genuinely different payload does.
    """
    skills = _injected_for(params)
    if not skills:
        return ""
    payload = sorted(
        (s.name, s.description, s.intent or "", str(s.path)) for s in skills)
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _injected_for(params: dict) -> List:
    """Supplied skill text, from either an inline object or a file path.

    The web form pastes JSON inline (no file to write first); the API also
    accepts ``injected_path`` so a caller with a truth file on disk can point
    at it. A malformed inline payload is dropped rather than failing the run
    — the audit still proceeds on the scanned catalog.
    """
    raw = params.get("injected")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if raw:
        return parse_injected(raw)
    path = params.get("injected_path")
    if isinstance(path, str) and path.strip():
        return load_injected(Path(path.strip()))
    return []


def injected_skills_payload(params: dict) -> List[dict]:
    """Describe the supplied skills for the result payload.

    Reads through the same ``_injected_for`` the audit uses, so the names the UI
    marks are exactly the skills that were merged in — including the ones that
    arrived via ``injected_path`` rather than pasted inline. ``intent`` rides
    along because the UI shows it, already labelled as a stated purpose rather
    than an observation.
    """
    return [
        {"name": s.name, "description": s.description, "intent": s.intent,
         "source": str(s.path)}
        for s in _injected_for(params)
    ]


def run_audit(job: Job) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        job.mark("parsing session log")
        source_path = Path(job.params["path"]).expanduser()
        session = load_session(source_path)
        if not session.events:
            raise ValueError("no events parsed")
        job.mark("building fact timeline (%d events)" % len(session.events))
        timeline = build_timeline(session)
        job.mark("loading skill catalog")
        skill_dirs = [Path(d).expanduser() for d in job.params.get("skills_dirs") or []]
        catalog = load_catalog(extra_dirs=skill_dirs)
        injected = _injected_for(job.params)
        if injected:
            catalog = merge_catalog(catalog, injected)
            job.mark("merged %d injected skills" % len(injected))
        # The IR document is built before judging: the gap candidate pool
        # (listed but never loaded, by IR evidence) is what the judge sees.
        job.mark("building IR document")
        from .ir.analyses import skill_loads as skill_loads_rollup
        from .ir.analyses import subagent_returns as subagent_returns_rollup
        from .ir.builder import build_audit_document
        from .ir.gap import gap_candidates, skill_gaps
        adapter_id = _ir_adapter_id(session, source_path)
        document = build_audit_document(session, adapter_id)
        candidate_pairs, gap_stats = gap_candidates(
            document.skills, document.items, catalog)
        judge = Judge.from_env()
        judgments: List[Judgment] = []
        judge_enabled = judge is not None
        if judge_enabled and job.params.get("judge_triggers"):
            job.mark("judging %d gap candidates (%s)"
                     % (len(candidate_pairs), judge.model))
            judgments = judge_session(
                timeline, [skill for _, skill in candidate_pairs], judge)
        if_results: List[IFResult] = []
        if judge_enabled and job.params.get("steps"):
            for playbook in job.params["steps"]:
                job.mark("evaluating instruction following: %s" % Path(playbook).name)
                if_results.append(evaluate_playbook(judge, Path(playbook), session))
        job.mark("deriving next steps")
        billed_points = []
        billed_series = []
        if job.params.get("billed"):
            from .billing import (BillingUnavailable, compaction_points_from_series,
                                  query_billed_series, session_uuid_from_source)
            billed_uuid = session_uuid_from_source(source_path)
            if billed_uuid is not None:
                try:
                    billed_series = query_billed_series(billed_uuid)
                except BillingUnavailable as exc:
                    job.mark("billed_compaction unavailable: %s" % exc.reason)
                billed_points = compaction_points_from_series(billed_series)
        compaction_windows, compaction_source = merge_compaction_windows(timeline, billed_points)
        token_stats = build_token_stats(
            session, timeline,
            compaction_windows=compaction_windows,
            compaction_source=compaction_source,
            billed_series=billed_series)
        expected = job.params.get("expect") or []
        consumed = {n.lower() for n in timeline.consumed_skill_names()}
        expectations = [
            {
                "name": name,
                "status": (
                    "loaded"
                    if name.lower() in {l.skill_name.lower() for l in timeline.loads}
                    else "file-read" if name.lower() in consumed else "missing"
                ),
            }
            for name in expected
        ]
        judgments_block = judgments_to_dict(judgments)
        missed = [f["skill_name"] for f in judgments_block["missed"]]
        load_block = skill_loads_rollup(document)
        returns_block = subagent_returns_rollup(document)
        gap_block = skill_gaps(
            document.skills, catalog, judgments, stats=gap_stats,
            judge_configured=judge_enabled)
        load_totals = load_block["totals"]
        token_stats_block = tokenstats_to_dict(token_stats)
        if_block = if_results_to_dict(if_results)
        bill = build_bill(
            token_stats_block, load_block, returns_block, expectations,
            judgments_block, if_block, billed_series,
            len(timeline.file_reads), adapter_id, judge_enabled)
        status_line = "%d loaded, %d file-read, %d missed triggers, IF %s, loads %d (%d costed, %d unavailable), reloads %d" % (
            len(timeline.loads),
            len(timeline.file_reads),
            len(missed),
            ", ".join(
                "%s %.2f" % (r.playbook, r.coverage) for r in if_results
            ) or "off",
            load_totals["loads"],
            load_totals["loads"] - load_totals["unavailable"],
            load_totals["unavailable"],
            load_totals["reloads"],
        )
        job.result = {
            "session": {
                "path": str(source_path),
                "turns": len(timeline.turns),
                "events": len(session.events),
            },
            "timeline": timeline_to_dict(timeline),
            "judgments": judgments_block,
            "if_results": if_block,
            "expectations": expectations,
            "event_feed": event_feed_to_list(session),
            "token_stats": token_stats_block,
            "skill_loads": load_block,
            "bill": bill,
            "subagent_returns": returns_block,
            "skill_gaps": gap_block,
            "no_self_invoke": sorted(
                s.name for s in catalog if s.disable_model_invocation
            ),
            "judge_enabled": judge_enabled,
            "catalog_size": len(catalog),
            "catalog_dirs": [str(d) for d in skill_dirs],
            "injected_count": len(injected),
            # Supplied skill text, named so the UI can mark those skills apart
            # from the ones the scanner found.
            "injected_skills": injected_skills_payload(job.params),
            "status_line": status_line,
            "suggestions": suggest_next_steps(
                timeline, expectations, judgments, if_results, judge_enabled, token_stats
            ),
            "duration_s": round(time.time() - job.started_at, 1),
        }
        job.mark("done in %.1fs" % (time.time() - job.started_at))
        job.status = "done"
        key = cache_key(job.params)
        fp = fingerprint(
            job.params["path"],
            (Judge.from_env().model if Judge.from_env() else "no-judge"),
        )
        cache_store(key, fp, job.result)
        job.result["cached"] = False
    except Exception as exc:
        job.status = "error"
        job.error = str(exc)
        job.mark("failed: %s" % exc)
    finally:
        job.finished_at = time.time()


def start_job(params: dict) -> Job:
    job = Job(params)
    with JOBS_LOCK:
        JOBS[job.id] = job
        # bound memory
        while len(JOBS) > 50:
            oldest = min(JOBS.values(), key=lambda j: j.started_at or time.time())
            JOBS.pop(oldest.id, None)
    threading.Thread(target=run_audit, args=(job,), daemon=True).start()
    return job


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, page: str) -> None:
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(
            path.suffix, "text/plain"
        )
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route in ("/", "/index.html"):
            self._send_file(WEBAPP_DIR / "index.html")
        elif route.startswith("/static/"):
            self._send_file(WEBAPP_DIR / route[len("/static/"):])
        elif route == "/api/sessions":
            params = parse_qs(parsed.query)
            self._send_json(200, list_sessions(query=(params.get("q") or [""])[0]))
        elif route == "/api/fleet":
            self._send_json(200, {"fleet": fleet_stats()})
        elif route.startswith("/api/job/"):
            job = JOBS.get(route.rsplit("/", 1)[1])
            if not job:
                self._send_json(404, {"error": "no such job"})
                return
            payload = {"id": job.id, "status": job.status, "error": job.error, "steps": job.steps}
            if job.status == "done":
                payload["result"] = job.result
            self._send_json(200, payload)
        elif route == "/api/playbooks":
            playbooks = sorted(
                str(p) for p in Path("~/.agents/skills").expanduser().glob("*/playbooks/*.md")
            )
            self._send_json(200, {"playbooks": playbooks})
        elif route == "/api/tree":
            params = parse_qs(parsed.query)
            path = (params.get("path") or [""])[0]
            if not path:
                self._send_json(400, {"error": "path required"})
                return
            if not Path(path).expanduser().exists():
                self._send_json(404, {"error": "session not found"})
                return
            try:
                page = render_tree_page(
                    path, billed=(params.get("billed") == ["1"]),
                    ide_db=(params.get("ide_db") == ["1"]))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            self._send_html(page)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/api/scan-dirs", "/api/catalog-audit"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": "bad json"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": "bad payload"})
                return
            if parsed.path == "/api/scan-dirs":
                dirs = payload.get("dirs") or []
                self._send_json(
                    200, scan_skill_dirs(dirs if isinstance(dirs, list) else []))
                return
            self._send_json(200, run_catalog_audit(payload))
            return
        if parsed.path == "/api/upload":
            params = parse_qs(parsed.query)
            name = (params.get("name") or [""])[0] or (
                "upload-%d.jsonl" % int(time.time()))
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_UPLOAD_BYTES:
                self._send_json(413, {"error": "upload too large"})
                return
            body = self.rfile.read(length) if length else b""
            try:
                self._send_json(200, store_upload(body, name))
            except UploadRejected as exc:
                self._send_json(
                    400, {"error": "not an analyzable session file: %s" % exc})
            except OSError as exc:
                self._send_json(500, {"error": "storing upload failed: %s" % exc})
            return
        if parsed.path != "/api/audit":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "bad json"})
            return
        if not params.get("path"):
            self._send_json(400, {"error": "path required"})
            return
        # cache short-circuit: same log fingerprint + same params + same
        # judge model serves the previous result instead of recomputing
        if not params.get("force"):
            judge = Judge.from_env()
            key = cache_key(params)
            cached = cache_load(key, fingerprint(params["path"], judge.model if judge else "no-judge"))
            if cached is not None:
                cached["cached"] = True
                self._send_json(200, {"cached": True, "result": cached})
                return
        job = start_job(params)
        self._send_json(202, {"job_id": job.id})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/upload":
            self._send_json(404, {"error": "not found"})
            return
        params = parse_qs(parsed.query)
        path = (params.get("path") or [""])[0]
        if not path:
            self._send_json(400, {"error": "path required"})
            return
        stored = delete_upload(path)
        if stored is None:
            self._send_json(404, {"error": "no such upload"})
            return
        self._send_json(200, {"deleted": stored})


def serve(port: int = 8471) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("agent-session-detective web: http://127.0.0.1:%d" % port)
    server.serve_forever()
