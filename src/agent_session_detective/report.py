"""Render the audit as a self-contained interactive HTML report.

No JavaScript and no external assets: expandables use native
<details>/<summary>. Every judgment links to the quoted evidence it rests
on; every skill content block is the content captured at load time (R9, R10).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import List, Optional

from .if_eval import IFResult
from .judge import Judgment
from .timeline import Timeline
from .tokenstats import BUCKET_KEYS, TokenStats
from .wire import Session

CSS = """
:root { --ink:#1a1d21; --dim:#5b6470; --line:#e3e6ea; --fact:#0b6bcb;
        --warn:#b25e09; --bad:#c0392b; --ok:#1e7d46; --bg:#f7f8fa; }
* { box-sizing:border-box; }
body { font:15px/1.6 -apple-system,"SF Pro","PingFang SC",sans-serif;
       color:var(--ink); margin:0; background:#fff; }
header { background:var(--bg); border-bottom:1px solid var(--line); padding:28px 36px; }
h1 { margin:0 0 6px; font-size:22px; }
h2 { font-size:17px; margin:36px 0 12px; padding-top:20px; border-top:1px solid var(--line); }
.meta { color:var(--dim); font-size:13px; }
main { max-width:960px; margin:0 auto; padding:8px 36px 64px; }
.badge { display:inline-block; font-size:12px; padding:1px 8px; border-radius:10px;
         background:var(--bg); border:1px solid var(--line); color:var(--dim); margin-right:6px; }
.badge.fact { color:var(--fact); border-color:var(--fact); }
.badge.infer { color:var(--warn); border-color:var(--warn); }
.badge.missed { color:var(--bad); border-color:var(--bad); }
.badge.ok { color:var(--ok); border-color:var(--ok); }
details { border:1px solid var(--line); border-radius:8px; margin:8px 0; }
summary { cursor:pointer; padding:10px 14px; font-weight:600; }
details[open] summary { border-bottom:1px solid var(--line); }
.body { padding:12px 14px; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:6px;
      padding:10px 12px; overflow-x:auto; font-size:12.5px; line-height:1.5; white-space:pre-wrap; }
blockquote { margin:8px 0; padding:6px 12px; border-left:3px solid var(--fact);
             background:var(--bg); }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
td,th { border:1px solid var(--line); padding:5px 10px; text-align:left; vertical-align:top; }
th { background:var(--bg); }
.notice { padding:10px 14px; border-radius:8px; background:#fdf3e7;
          border:1px solid var(--warn); color:var(--warn); }
.timeline-item { display:flex; gap:12px; padding:8px 0; border-bottom:1px dashed var(--line); }
.timeline-ts { color:var(--dim); font-size:12.5px; white-space:nowrap; width:150px; }
.empty { color:var(--dim); font-style:italic; }
"""


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _context_sparkline(timeline: Timeline) -> str:
    points = [
        (e.payload.get("context_tokens") or 0, e.payload.get("context_usage") or 0)
        for e in timeline.status_series
    ]
    if len(points) < 2:
        return ""
    width, height = 920, 90
    max_tokens = max(p[0] for p in points) or 1
    coords = " ".join(
        "%d,%d" % (i * width // (len(points) - 1), height - int(p[0] / max_tokens * (height - 10)))
        for i, p in enumerate(points)
    )
    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" role="img" '
        'aria-label="context token usage over time">'
        '<polyline fill="none" stroke="var(--fact)" stroke-width="1.5" points="%s"/></svg>'
        % (width, height, height, coords)
    )


BUCKET_STYLE = {
    "system": ("var(--warn)", "system resident (residual, inferred)"),
    "history": ("var(--fact)", "dialogue history (user turns, estimate)"),
    "injected": ("var(--bad)", "retrieval/tool injection (tool results, estimate)"),
    "output": ("var(--ok)", "model output (content parts, estimate)"),
}


def _growth_chart(stats: TokenStats) -> str:
    """Stacked per-turn bars: how many tokens each turn added, by bucket."""
    rows = [
        r for r in stats.turn_growth
        if r.added is not None and r.added >= 0 and not r.crossed_compaction
    ]
    if len(rows) < 2:
        return ""
    width, height = 920, 150
    max_added = max(r.added or 0 for r in rows) or 1
    bar_w = max(1, width // len(rows))
    parts = [
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" role="img" '
        'aria-label="tokens added per turn, stacked by bucket">' % (width, height, height)
    ]
    for i, r in enumerate(rows):
        x = i * bar_w
        y = height
        observed = r.system_added + r.history_added + r.injected_added + r.output_added
        # token estimates can overshoot the measured delta; scale the bar
        # back down so segments stay proportional to the actual growth
        scale = min(1.0, (r.added or 0) / observed) if observed else 1.0
        for key in BUCKET_KEYS:
            val = getattr(r, key + "_added")
            h = int(val * scale / max_added * (height - 20))
            if h <= 0:
                continue
            y -= h
            parts.append(
                '<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                % (x, y, max(1, bar_w - 1), h, BUCKET_STYLE[key][0])
            )
    parts.append("</svg>")
    return "".join(parts)


def _render_token_governance(stats: TokenStats) -> str:
    out: List[str] = ["<h2>Token Governance</h2>"]

    badges = []
    if stats.usage_records:
        hit = "%.1f%%" % (stats.cache_hit_rate * 100) if stats.cache_hit_rate is not None else "?"
        badges.append("<span class='badge fact'>cache hit: %s</span>" % hit)
        badges.append("<span class='badge'>input billed: %d</span>" % stats.input_total)
        badges.append("<span class='badge'>output total: %d</span>" % stats.output_total)
    else:
        badges.append("<span class='badge'>no usage records in this log</span>")
    verdict_cls = "ok" if stats.growth_verdict.startswith("sublinear") else (
        "missed" if stats.growth_verdict.startswith(("accelerat", "linear")) else ""
    )
    badges.append("<span class='badge %s'>growth: %s</span>" % (verdict_cls, esc(stats.growth_verdict)))
    if stats.hash_flips is None:
        badges.append("<span class='badge'>prompt flips: unavailable (no LLM request hashes)</span>")
    else:
        badges.append(
            "<span class='badge %s'>prompt flips: %d</span>"
            % ("missed" if stats.hash_flips else "ok", stats.hash_flips)
        )
    if stats.repeats:
        badges.append(
            "<span class='badge infer'>repeat injection: ~%d tokens</span>"
            % stats.repeat_extra_tokens
        )
    out.append("<div>%s</div>" % " ".join(badges))

    chart = _growth_chart(stats)
    if chart:
        out.append(chart)
        legend = " · ".join(
            "<span style='color:%s'>%s</span>" % (BUCKET_STYLE[k][0], esc(BUCKET_STYLE[k][1]))
            for k in BUCKET_KEYS
        )
        out.append("<p class='meta'>tokens added per turn (turns crossed by a compaction "
                   "excluded): %s</p>" % legend)
        out.append(
            "<p class='meta'>per-turn context at turn start: %s</p>"
            % esc(", ".join(
                "T%d=%d" % (r.turn, r.context_at_start) for r in stats.turn_growth[:30]
            ))
        )

    if stats.bucket_shares:
        out.append("<table><tr><th>bucket</th><th>tokens added</th><th>share</th></tr>")
        for key in BUCKET_KEYS:
            color, label = BUCKET_STYLE[key]
            out.append(
                "<tr><td><span style='color:%s'>%s</span></td><td>%d</td><td>%.1f%%</td></tr>"
                % (color, esc(label), stats.bucket_totals[key], stats.bucket_shares[key] * 100)
            )
        out.append("</table>")
        out.append(
            "<p class='meta'>buckets are token estimates from event content; the system "
            "bucket is the residual of context growth minus observed content (inferred), "
            "so estimation error lands there too. Desktop logs measure per-turn context "
            "exactly; CLI logs interpolate from the status series.</p>"
        )

    if stats.hash_runs:
        out.append("<table><tr><th>kind</th><th>hash</th><th>requests</th><th>first</th><th>last</th></tr>")
        for run in stats.hash_runs:
            out.append(
                "<tr><td>%s</td><td><code>%s</code></td><td>%d</td><td>%s</td><td>%s</td></tr>"
                % (esc(run.kind), esc(run.hash[:12]), run.requests,
                   fmt_ts(run.first_ts), fmt_ts(run.last_ts))
            )
        out.append("</table>")
        if stats.hash_flips:
            out.append(
                "<p class='notice'>prompt prefix changed mid-session %d time(s) — a log fact "
                "of hash flips. Inference to label: each flip invalidates the provider's "
                "prefix cache and re-pays full prefill for the system prompt and tools.</p>"
                % stats.hash_flips
            )

    if stats.repeats:
        out.append("<details><summary>repeat injection (%d groups, ~%d extra tokens)"
                   "</summary><div class='body'><table>"
                   "<tr><th>occurrences</th><th>~tokens each</th><th>extra tokens</th><th>content</th></tr>"
                   % (len(stats.repeats), stats.repeat_extra_tokens))
        for r in stats.repeats:
            out.append(
                "<tr><td>%d</td><td>%d</td><td>%d</td><td>%s</td></tr>"
                % (r.occurrences, r.tokens_each, r.extra_tokens, esc(r.preview[:120]))
            )
        out.append("</table><p class='meta'>identical tool results (≥%d chars) seen more "
                   "than once. Re-injection is the observable signature of state not being "
                   "externalized — a derived summary should replace the replay.</p>"
                   "</div></details>" % 200)
    return "\n".join(out)


def render_report(
    session: Session,
    timeline: Timeline,
    judgments: List[Judgment],
    judge_enabled: bool,
    catalog_size: int,
    expected: Optional[List[str]] = None,
    if_results: Optional[List["IFResult"]] = None,
    token_stats: Optional[TokenStats] = None,
    no_self_invoke: Optional[set] = None,
) -> str:
    if_results = if_results or []
    missed = [j for j in judgments if j.triggered]
    errors = [j for j in judgments if j.error]
    total_turns = len(timeline.turns)

    loaded_lower = {n.lower() for n in timeline.skill_names()}
    read_lower = {f.skill_name.lower() for f in timeline.file_reads}

    parts = [
        "<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Agent Skill Audit</title><style>%s</style></head><body>" % CSS,
        "<header><h1>Agent Skill Audit</h1>",
        "<div class='meta'>%s</div>" % esc(session.directory),
        "<div style='margin-top:10px'>",
        "<span class='badge'>turns: %d</span>" % total_turns,
        "<span class='badge'>events: %d</span>" % len(session.events),
        "<span class='badge fact'>skills loaded: %d</span>" % len(timeline.loads),
        "<span class='badge'>skill files read: %d</span>" % len(timeline.file_reads),
        "<span class='badge'>compactions: %s</span>" % (
            len(timeline.compactions) if timeline.compaction_telemetry_available else "unavailable"
        ),
        "<span class='badge'>catalog: %d skills</span>" % catalog_size,
        "<span class='badge ok'>judge: %s</span>" % ("on" if judge_enabled else "off"),
        "</div></header><main>",
    ]

    # --- Expectations checklist ---
    if expected:
        parts.append("<h2>Expectations</h2>")
        parts.append("<table><tr><th>expected skill</th><th>status</th></tr>")
        for name in expected:
            key = name.lower()
            if key in loaded_lower:
                status = '<span class="badge ok">loaded</span>'
            elif key in read_lower:
                status = '<span class="badge infer">file-read only</span>'
            else:
                status = '<span class="badge missed">MISSING</span>'
            parts.append("<tr><td>%s</td><td>%s</td></tr>" % (esc(name), status))
        parts.append("</table>")

    # --- Findings ---
    parts.append("<h2>Findings</h2>")
    if not judge_enabled:
        parts.append(
            "<div class='notice'>Trigger judging is off. Set %s, %s and %s to enable it.</div>"
            % ("ASD_JUDGE_BASE_URL", "ASD_JUDGE_API_KEY", "ASD_JUDGE_MODEL")
        )
    elif missed:
        for j in missed:
            parts.append(
                "<details><summary><span class='badge missed'>missed trigger</span> %s "
                "<span class='meta'>turn %s · confidence %.2f</span></summary><div class='body'>"
                % (esc(j.skill_name), esc(j.turn), j.confidence)
            )
            parts.append("<p>%s</p>" % esc(j.rationale))
            if j.evidence:
                parts.append("<blockquote>%s</blockquote>" % esc(j.evidence))
            parts.append("</div></details>")
    else:
        parts.append("<p class='empty'>No missed triggers found among the judged skills.</p>")
    if errors:
        parts.append(
            "<p class='meta'>%d judgments discarded as tool defects (no verifiable evidence): %s</p>"
            % (len(errors), esc(", ".join(sorted({e.error or "" for e in errors}))))
        )

    # --- Instruction Following ---
    if if_results:
        parts.append("<h2>Instruction Following</h2>")
        parts.append(
            "<p class='meta'>Step verdicts anchored to trajectory evidence (actions and "
            "results only; plans and claims do not count). Coverage is recomputed from "
            "verdicts, not taken from the judge. Gate: 0.75 = Mostly Followed.</p>"
        )
        for r in if_results:
            if r.not_applicable:
                parts.append(
                    "<p><span class='badge'>%s</span> <span class='meta'>no enumerable "
                    "steps; not applicable, excluded from aggregation</span></p>" % esc(r.playbook)
                )
                continue
            verdict_badge = (
                '<span class="badge ok">PASS %.2f</span>' if r.passed
                else '<span class="badge missed">FAIL %.2f</span>'
            ) % r.coverage
            parts.append(
                "<details open><summary>%s %s <span class='meta'>gate %.2f · %d steps</span>"
                "</summary><div class='body'><table>"
                "<tr><th>#</th><th>step</th><th>status</th><th>evidence</th></tr>"
                % (esc(r.playbook), verdict_badge, r.gate, len(r.verdicts))
            )
            for i, v in enumerate(r.verdicts, 1):
                badge_cls = {"covered": "ok", "partial": "infer", "skipped": "missed"}[v.status]
                parts.append(
                    "<tr><td>%d</td><td>%s</td><td><span class='badge %s'>%s</span></td>"
                    "<td>%s%s</td></tr>"
                    % (
                        i, esc(v.step[:160]), badge_cls, v.status,
                        esc(v.evidence[:200]),
                        ("<br><span class='meta'>%s</span>" % esc(v.rationale[:160])) if v.rationale else "",
                    )
                )
            parts.append("</table></div></details>")

    # --- SKILL.md file reads outside the Skill mechanism ---
    if timeline.file_reads:
        parts.append("<h2>SKILL.md Read as Plain Files</h2>")
        parts.append(
            "<p class='meta'>These SKILL.md files were read with the file tool, never loaded "
            "through the Skill mechanism. Reading is a log fact; whether it was an adequate "
            "substitute for a formal load is for the reader to judge.</p>"
        )
        for read in timeline.file_reads:
            origin = "" if read.origin == "main" else '<span class="badge">%s</span>' % esc(read.origin)
            parts.append(
                "<details><summary><span class='badge infer'>file read</span> %s %s "
                "<span class='meta'>%s · %s</span></summary><div class='body'>"
                % (esc(read.skill_name), origin, fmt_ts(read.ts), esc(read.path))
            )
            if read.snippet:
                parts.append("<pre>%s</pre>" % esc(read.snippet[:2000]))
            parts.append("</div></details>")

    # --- Skill lifecycle timeline ---
    parts.append("<h2>Skill Lifecycle</h2>")
    if not timeline.loads:
        parts.append(
            "<p class='empty'>No skill loads detected in this session. "
            "Skill loads are recognized as Skill tool calls in the log; if the agent "
            "loads skills differently, this is a parser gap, not proof of absence.</p>"
        )
    for load in timeline.loads:
        eviction = (
            '<span class="badge infer">possibly evicted by compaction #%d (inference, not a log fact)</span>'
            % (load.evicted_by + 1)
            if load.evicted
            else '<span class="badge ok">in context at session end</span>'
        )
        no_self = ""
        if no_self_invoke and load.skill_name.lower() in no_self_invoke:
            no_self = (
                "<span class='badge infer'>declared disable-model-invocation: the log cannot "
                "show whether this load was named by the router or self-invoked — verify "
                "against the routing rules</span>"
            )
        origin = "" if load.origin == "main" else '<span class="badge">%s</span>' % esc(load.origin)
        parts.append(
            "<details><summary><span class='badge fact'>loaded</span> %s %s%s "
            "<span class='meta'>%s · ~%d tokens</span></summary><div class='body'>"
            % (
                esc(load.skill_name), origin, eviction, fmt_ts(load.ts), load.tokens_est,
            )
        )
        ctx = (
            "context after load: %s tokens (%.1f%%)"
            % (load.context_tokens_after, (load.context_usage_after or 0) * 100)
            if load.context_tokens_after
            else "context after load: unknown"
        )
        parts.append("<p class='meta'>%s · source: %s line %d</p>" % (ctx, esc(load.source.name), load.source_line))
        if no_self:
            parts.append("<p>%s</p>" % no_self)
        if load.is_error:
            parts.append("<p><span class='badge missed'>load returned an error</span></p>")
        if load.content:
            parts.append("<pre>%s</pre>" % esc(load.content[:4000]))
        parts.append("</div></details>")

    # --- Compaction markers ---
    if timeline.compactions:
        parts.append("<h2>Compactions</h2>")
        parts.append("<table><tr><th>#</th><th>begin</th><th>end</th></tr>")
        for c in timeline.compactions:
            parts.append(
                "<tr><td>%d</td><td>%s</td><td>%s</td></tr>"
                % (c.index + 1, fmt_ts(c.begin_ts), fmt_ts(c.end_ts))
            )
        parts.append("</table>")

    # --- Context usage ---
    spark = _context_sparkline(timeline)
    if spark:
        parts.append("<h2>Context Usage</h2>")
        parts.append(spark)
        parts.append(
            "<p class='meta'>context_tokens after each status update; "
            "the log does not record which content survived a compaction.</p>"
        )

    # --- Token governance ---
    if token_stats is not None:
        parts.append(_render_token_governance(token_stats))

    # --- Raw event feed ---
    parts.append("<h2>Event Feed</h2>")
    for event in session.events:
        if event.type in ("ContentPart",):
            continue
        origin = "" if event.origin == "main" else '[%s] ' % esc(event.origin)
        parts.append(
            "<div class='timeline-item'><div class='timeline-ts'>%s</div><div>%s%s</div></div>"
            % (fmt_ts(event.ts), origin, esc(event.text_preview()))
        )

    parts.append("</main></body></html>")
    return "\n".join(parts)
