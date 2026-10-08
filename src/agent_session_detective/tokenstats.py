"""Token governance statistics: cache hit rate, per-turn growth shape,
bucket mix, prompt-prefix stability, repeat injection.

Facts come from usage records (desktop ``usage.record``, CLI
``StatusUpdate.token_usage``) and the context-size series. Buckets are
estimates: event content is token-estimated, and the system bucket is the
residual of "context growth minus observed content" — always labeled as
inferred, never presented as log fact. Turns crossed by a compaction are
excluded from growth and bucket math because the delta no longer reflects
content addition.

``skill`` and ``inject`` are kept out of ``tool`` on purpose: a skill body is
resident instruction and a harness wrapper is context the person never typed,
so folding either into the tool-result bucket hides exactly the footprint a
skill audit exists to measure.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .timeline import Timeline, estimate_tokens, is_skill_call, skill_name_from_content
from .wire import Event, Session

# Tool results smaller than this are not checked for repeat injection.
MIN_REPEAT_CHARS = 200

# Harness-authored blocks riding inside a user message. Injection is
# recognised by content signature, not by the call envelope: the envelope is
# an ordinary user turn either way, so only the wrapper gives it away.
INJECT_BLOCK_RE = re.compile(
    r"<(system-reminder|system_reminder|restored_files|user-prompt-submit-hook"
    r"|command-name|command-message|command-args|local-command-stdout)\b[^>]*>"
    r".*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)

BUCKET_KEYS = ("system", "history", "inject", "skill", "tool", "output")


@dataclass
class UsageRecord:
    ts: Optional[float]
    origin: str
    model: str
    input_other: int
    output: int
    input_cache_read: int
    input_cache_creation: int

    @property
    def input_total(self) -> int:
        return self.input_other + self.input_cache_read + self.input_cache_creation


@dataclass
class PromptHashRun:
    """One uninterrupted stretch of requests sharing a prompt/tools hash."""

    kind: str  # "system" | "tools"
    hash: str
    first_ts: Optional[float]
    last_ts: Optional[float]
    requests: int


@dataclass
class TurnItem:
    """One observable content contribution inside a turn (drill-down)."""

    bucket: str
    tokens: int
    preview: str


@dataclass
class TurnGrowth:
    turn: int  # 1-based
    ts: Optional[float]
    context_at_start: int
    exact: bool  # False = interpolated from the status series (CLI format)
    added: Optional[int] = None  # context growth until the next turn starts
    system_added: int = 0  # residual, inferred
    history_added: int = 0  # what the person typed, token estimate
    inject_added: int = 0  # harness-authored blocks in a user turn
    skill_added: int = 0  # skill bodies, by call name or content signature
    tool_added: int = 0  # every other tool result
    output_added: int = 0  # model content parts, token estimate
    crossed_compaction: bool = False
    items: List[TurnItem] = field(default_factory=list)


@dataclass
class Repeat:
    preview: str
    occurrences: int
    tokens_each: int
    extra_tokens: int  # (occurrences - 1) * tokens_each


@dataclass
class TokenStats:
    usage_records: List[UsageRecord] = field(default_factory=list)
    input_total: int = 0
    output_total: int = 0
    cache_read_total: int = 0
    cache_creation_total: int = 0
    cache_hit_rate: Optional[float] = None
    turn_growth: List[TurnGrowth] = field(default_factory=list)
    growth_verdict: str = "insufficient data"
    bucket_totals: Dict[str, int] = field(default_factory=dict)
    bucket_shares: Dict[str, float] = field(default_factory=dict)
    hash_runs: List[PromptHashRun] = field(default_factory=list)
    hash_flips: Optional[int] = None
    repeats: List[Repeat] = field(default_factory=list)
    repeat_extra_tokens: int = 0


def _usage_from_events(events: List[Event]) -> List[UsageRecord]:
    out: List[UsageRecord] = []
    for e in events:
        if e.type == "UsageRecord":
            p = e.payload
            out.append(
                UsageRecord(
                    ts=e.ts,
                    origin=e.origin,
                    model=str(p.get("model") or ""),
                    input_other=int(p.get("input_other") or 0),
                    output=int(p.get("output") or 0),
                    input_cache_read=int(p.get("input_cache_read") or 0),
                    input_cache_creation=int(p.get("input_cache_creation") or 0),
                )
            )
        elif e.type == "StatusUpdate":
            tu = e.payload.get("token_usage")
            if isinstance(tu, dict):
                out.append(
                    UsageRecord(
                        ts=e.ts,
                        origin=e.origin,
                        model="",
                        input_other=int(tu.get("input_other") or 0),
                        output=int(tu.get("output") or 0),
                        input_cache_read=int(tu.get("input_cache_read") or 0),
                        input_cache_creation=int(tu.get("input_cache_creation") or 0),
                    )
                )
    out.sort(key=lambda r: (r.ts is None, r.ts or 0.0))
    return out


def _hash_runs(events: List[Event]) -> Tuple[List[PromptHashRun], Optional[int]]:
    """Collapse LLMRequest events (main agent only) into per-hash runs.

    A hash flip is a log fact: the request prefix changed. That it broke a
    provider cache is the inference the report layer labels as such. A log
    without request hashes cannot establish a zero-flip fact.
    """
    runs: List[PromptHashRun] = []
    last_run: Dict[str, PromptHashRun] = {}
    prev_hash: Dict[str, str] = {}
    flips = 0
    has_request_hash = False
    for e in events:
        if e.type != "LLMRequest" or e.origin != "main":
            continue
        p = e.payload
        for kind, key in (("system", "system_prompt_hash"), ("tools", "tools_hash")):
            h = str(p.get(key) or "")
            if not h:
                continue
            has_request_hash = True
            run = last_run.get(kind)
            if run is not None and run.hash == h:
                run.requests += 1
                run.last_ts = e.ts
            else:
                if kind in prev_hash:
                    flips += 1
                prev_hash[kind] = h
                run = PromptHashRun(kind=kind, hash=h, first_ts=e.ts, last_ts=e.ts, requests=1)
                runs.append(run)
                last_run[kind] = run
    return runs, flips if has_request_hash else None


def _turn_contexts(session: Session) -> List[Tuple[int, bool]]:
    """Context size at each user turn's start: (tokens, exact).

    Desktop logs measure this directly (token_counting.turn_recorded); the
    current turn typically has no record yet, so missing turns fall back to
    interpolating the nearest earlier status measurement (exact=False). CLI
    logs only have the status series, so every turn is interpolated.
    """
    turns = [t for t in session.turns() if t.origin == "main"]
    exact: Dict[int, int] = {}
    for e in session.events:
        if e.type == "TurnTokens":
            exact[int(e.payload.get("turn_id") or 0)] = int(e.payload.get("tokens") or 0)
    statuses = [
        (e.ts, int(e.payload.get("context_tokens") or 0))
        for e in session.events
        if e.type == "StatusUpdate" and e.origin == "main" and e.payload.get("context_tokens")
    ]

    def interpolated(t_ts: Optional[float]) -> int:
        before = [s for s in statuses if s[0] is not None and t_ts is not None and s[0] <= t_ts]
        return before[-1][1] if before else (statuses[0][1] if statuses else 0)

    out: List[Tuple[int, bool]] = []
    for i, t in enumerate(turns):
        if i in exact:
            out.append((exact[i], True))
        elif exact:
            out.append((interpolated(t.ts), False))
        else:
            out.append((interpolated(t.ts), False))
    return out


def _event_text(e: Event) -> str:
    if e.type == "ToolResult":
        return str(e.payload.get("return_value", {}).get("output") or "")
    if e.type == "ContentPart":
        kind = e.payload.get("type")
        return str(e.payload.get(kind) or "") if kind in ("think", "text") else ""
    return ""


def _preview(text: str, limit: int = 160) -> str:
    one_line = " ".join(str(text).split())
    return one_line[:limit]


def _split_user_input(event: Event) -> Tuple[str, str]:
    """(what the person typed, what the harness wrapped around it)."""
    human: List[str] = []
    injected: List[str] = []
    for part in event.payload.get("user_input") or []:
        if not isinstance(part, dict):
            continue
        text = str(part.get("text") or "")
        if not text:
            continue
        injected.extend(m.group(0) for m in INJECT_BLOCK_RE.finditer(text))
        human.append(INJECT_BLOCK_RE.sub("", text))
    return "".join(human), "".join(injected)


def _has_context_measurement(session: Session) -> bool:
    return any(
        e.origin == "main"
        and (
            (e.type == "StatusUpdate" and e.payload.get("context_tokens") is not None)
            or (e.type == "TurnTokens" and e.payload.get("tokens") is not None)
        )
        for e in session.events
    )


def _turn_growth(session: Session, timeline: Timeline) -> List[TurnGrowth]:
    # Context growth requires an actual context measurement. Do not turn an
    # absent series (as in Qoder transcripts) into zero-delta pseudo-data.
    if not _has_context_measurement(session):
        return []

    # Subagent loops have their own context windows; per-turn growth and
    # buckets account for the main agent only.
    turns = [t for t in session.turns() if t.origin == "main"]
    contexts = _turn_contexts(session)
    n = len(turns)
    comp_starts = [c.begin_ts for c in timeline.compactions if c.begin_ts]
    skill_ids = {
        str(e.payload.get("id"))
        for e in session.events
        if e.origin == "main" and is_skill_call(e)
    }
    sums = [
        {"history": 0, "inject": 0, "skill": 0, "tool": 0, "output": 0}
        for _ in range(n)
    ]
    items: List[List[TurnItem]] = [[] for _ in range(n)]
    for e in session.events:
        if e.origin != "main" or e.ts is None or n == 0:
            continue
        idx: Optional[int] = None
        for i in range(n):
            t_ts = turns[i].ts
            if t_ts is not None and t_ts <= e.ts:
                idx = i
        if idx is None:
            continue
        if e.type == "TurnBegin":
            for bucket, text in zip(("history", "inject"), _split_user_input(e)):
                tokens = estimate_tokens(text)
                if not tokens:
                    continue
                sums[idx][bucket] += tokens
                items[idx].append(TurnItem(bucket, tokens, _preview(text)))
        elif e.type == "ToolResult":
            text = _event_text(e)
            tokens = estimate_tokens(text)
            call_id = str(e.payload.get("tool_call_id"))
            # A skill body is not a tool result: it is resident instruction.
            # Envelope first, content signature as the fallback arm.
            bucket = (
                "skill"
                if call_id in skill_ids or skill_name_from_content(text)
                else "tool"
            )
            sums[idx][bucket] += tokens
            items[idx].append(TurnItem(bucket, tokens, _preview(text)))
        elif e.type == "ContentPart":
            text = _event_text(e)
            if text:
                sums[idx]["output"] += estimate_tokens(text)
                items[idx].append(TurnItem("output", estimate_tokens(text), _preview(text)))

    rows: List[TurnGrowth] = []
    for i in range(n):
        ctx, exact = contexts[i] if i < len(contexts) else (0, False)
        row = TurnGrowth(turn=i + 1, ts=turns[i].ts, context_at_start=ctx, exact=exact)
        if i + 1 < n and i + 1 < len(contexts):
            row.added = contexts[i + 1][0] - ctx
            lo, hi = turns[i].ts, turns[i + 1].ts
            row.crossed_compaction = any(
                c is not None and lo is not None and hi is not None and lo <= c <= hi
                for c in comp_starts
            )
            if not row.crossed_compaction and row.added >= 0:
                observed = sums[i]
                row.history_added = observed["history"]
                row.inject_added = observed["inject"]
                row.skill_added = observed["skill"]
                row.tool_added = observed["tool"]
                row.output_added = observed["output"]
                # Whatever grew the context beyond observable content is
                # attributed to the system bucket — residual, inferred.
                row.system_added = max(0, row.added - sum(observed.values()))
        row.items = items[i]
        rows.append(row)
    return rows


def _growth_verdict(rows: List[TurnGrowth]) -> str:
    vals = [
        (r.turn, r.added)
        for r in rows
        if r.added is not None and r.added >= 0 and not r.crossed_compaction
    ]
    if len(vals) < 4:
        return "insufficient data (%d turns)" % len(vals)
    n = len(vals)
    xs = [float(v[0]) for v in vals]
    ys = [float(v[1]) for v in vals]
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    denom = sum((x - xbar) ** 2 for x in xs)
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / denom if denom else 0.0
    rel = slope / (ybar or 1.0)
    if rel > 0.2:
        return "accelerating (per-turn growth is rising)"
    if rel < -0.2:
        return "sublinear (per-turn growth is shrinking)"
    return "linear (constant per-turn growth)"


def _repeats(events: List[Event]) -> Tuple[List[Repeat], int]:
    """Identical large tool results seen more than once.

    Re-injecting the same content across turns is the observable signature
    of state not being externalized (summaries derived instead of replayed).
    """
    groups: Dict[str, dict] = {}
    for e in events:
        if e.type != "ToolResult":
            continue
        out = _event_text(e)
        if len(out) < MIN_REPEAT_CHARS:
            continue
        key = hashlib.sha1(out.encode("utf-8", "replace")).hexdigest()
        g = groups.setdefault(
            key,
            {"preview": out[:160], "occurrences": 0, "tokens": estimate_tokens(out)},
        )
        g["occurrences"] += 1
    repeats = [
        Repeat(
            preview=g["preview"],
            occurrences=g["occurrences"],
            tokens_each=g["tokens"],
            extra_tokens=(g["occurrences"] - 1) * g["tokens"],
        )
        for g in groups.values()
        if g["occurrences"] > 1
    ]
    repeats.sort(key=lambda r: -r.extra_tokens)
    return repeats[:10], sum(r.extra_tokens for r in repeats)


def build_token_stats(session: Session, timeline: Timeline) -> TokenStats:
    stats = TokenStats()
    stats.usage_records = _usage_from_events(session.events)
    stats.input_total = sum(u.input_total for u in stats.usage_records)
    stats.output_total = sum(u.output for u in stats.usage_records)
    stats.cache_read_total = sum(u.input_cache_read for u in stats.usage_records)
    stats.cache_creation_total = sum(u.input_cache_creation for u in stats.usage_records)
    if stats.input_total:
        stats.cache_hit_rate = stats.cache_read_total / stats.input_total

    stats.hash_runs, stats.hash_flips = _hash_runs(session.events)
    stats.turn_growth = _turn_growth(session, timeline)
    stats.growth_verdict = (
        _growth_verdict(stats.turn_growth)
        if _has_context_measurement(session)
        else "unavailable (no context telemetry)"
    )
    stats.repeats, stats.repeat_extra_tokens = _repeats(session.events)

    totals = {k: 0 for k in BUCKET_KEYS}
    for r in stats.turn_growth:
        if r.added is None or r.added < 0 or r.crossed_compaction:
            continue
        for key in BUCKET_KEYS:
            totals[key] += getattr(r, key + "_added")
    total_added = sum(totals.values())
    if total_added:
        stats.bucket_totals = totals
        stats.bucket_shares = {k: v / total_added for k, v in totals.items()}
    return stats
