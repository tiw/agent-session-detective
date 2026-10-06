"""Local web application: browse sessions, run audits, read reports.

Stdlib only. JSON API served by http.server; the UI is a single-page app
under webapp/. Audit jobs run on a background thread and are polled by id.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from .catalog import load_catalog
from .if_eval import IFResult, evaluate_playbook
from .judge import Judge, Judgment, judge_session
from .timeline import Timeline, build_timeline
from .tokenstats import TokenStats, build_token_stats
from .wire import Session, find_latest_session, load_session

WEBAPP_DIR = Path(__file__).parent / "webapp"

DEFAULT_ROOTS = ["~/.kimi-code/sessions", "~/.kimi/sessions"]

CACHE_DIR = Path("~/.cache/agent-session-detective").expanduser()


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
        },
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint(session_path: str, judge_model: str) -> str:
    """Log identity: newest mtime + total size of every wire file, plus the
    judge model (different model, different verdicts)."""
    base = Path(session_path).expanduser()
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
    return "%.3f:%d:%s:v2" % (newest, total, judge_model)


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
                "system_added": r.system_added,
                "history_added": r.history_added,
                "injected_added": r.injected_added,
                "output_added": r.output_added,
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
            }
            for r in s.repeats
        ],
        "repeat_extra_tokens": s.repeat_extra_tokens,
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
    return sorted(found.values(), key=lambda s: -s["mtime"])


SIDEBAR_LIMIT = 10


def list_sessions(roots: Optional[List[str]] = None) -> dict:
    """The most recent sessions for the sidebar. Parsing a session's log is
    deferred to the audit job; this endpoint must stay instant."""
    audited = cached_paths()
    all_sessions = discover_sessions(roots)
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
            stats = build_token_stats(session, timeline)
            s["cache_hit_rate"] = stats.cache_hit_rate
            s["growth_verdict"] = stats.growth_verdict
            s["repeat_extra_tokens"] = stats.repeat_extra_tokens
            s["output_total"] = stats.output_total
        except Exception:
            s["turns"] = s["events"] = s["skills_loaded"] = s["skill_files_read"] = None
            s["cache_hit_rate"] = s["growth_verdict"] = None
            s["repeat_extra_tokens"] = s["output_total"] = None
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


def run_audit(job: Job) -> None:
    job.status = "running"
    job.started_at = time.time()
    try:
        job.mark("parsing wire.jsonl")
        session = load_session(Path(job.params["path"]).expanduser())
        if not session.events:
            raise ValueError("no events parsed")
        job.mark("building fact timeline (%d events)" % len(session.events))
        timeline = build_timeline(session)
        job.mark("loading skill catalog")
        catalog = load_catalog()
        judge = Judge.from_env()
        judgments: List[Judgment] = []
        judge_enabled = judge is not None
        if judge_enabled and job.params.get("judge_triggers"):
            unconsumed = max(len(catalog) - len(timeline.consumed_skill_names()), 0)
            job.mark("judging %d unconsumed skills (%s)" % (unconsumed, judge.model))
            judgments = judge_session(timeline, catalog, judge)
        if_results: List[IFResult] = []
        if judge_enabled and job.params.get("steps"):
            for playbook in job.params["steps"]:
                job.mark("evaluating instruction following: %s" % Path(playbook).name)
                if_results.append(evaluate_playbook(judge, Path(playbook), session))
        job.mark("deriving next steps")
        token_stats = build_token_stats(session, timeline)
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
        missed = [f["skill_name"] for f in judgments_to_dict(judgments)["missed"]]
        status_line = "%d loaded, %d file-read, %d missed triggers, IF %s" % (
            len(timeline.loads),
            len(timeline.file_reads),
            len(missed),
            ", ".join(
                "%s %.2f" % (r.playbook, r.coverage) for r in if_results
            ) or "off",
        )
        job.result = {
            "session": {
                "path": str(session.directory),
                "turns": len(timeline.turns),
                "events": len(session.events),
            },
            "timeline": timeline_to_dict(timeline),
            "judgments": judgments_to_dict(judgments),
            "if_results": if_results_to_dict(if_results),
            "expectations": expectations,
            "event_feed": event_feed_to_list(session),
            "token_stats": tokenstats_to_dict(token_stats),
            "no_self_invoke": sorted(
                s.name for s in catalog if s.disable_model_invocation
            ),
            "judge_enabled": judge_enabled,
            "catalog_size": len(catalog),
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
            self._send_json(200, list_sessions())
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
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
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


def serve(port: int = 8471) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("agent-session-detective web: http://127.0.0.1:%d" % port)
    server.serve_forever()
