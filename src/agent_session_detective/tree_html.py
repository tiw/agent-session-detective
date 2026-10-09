"""Render the actual skill tree as a self-contained HTML page.

No JavaScript and no external assets, same rules as report.py. Every
number is either counted from the document or the literal word
``unavailable`` — a stub-derived cost is never shown (design R9).
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Dict, List, Optional

from .ir.schema import AuditDocument
from .ir.skill_tree import skill_tree

CSS = """
:root { --ink:#1a1d21; --dim:#5b6470; --line:#e3e6ea; --fact:#0b6bcb;
        --warn:#b25e09; --bad:#c0392b; --ok:#1e7d46; --bg:#f7f8fa;
        --inject:#7048e8; --skill:#c2255c; }
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

MAX_DEPTH = 12


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _cost(value: Optional[int]) -> str:
    if value is None:
        return "<span class='badge missed'>unavailable</span>"
    return "~%d EST" % value


def _footer(document: AuditDocument) -> str:
    loads = document.skill_loads
    n_loads = sum(1 for load in loads if load.kind == "load")
    n_reloads = sum(1 for load in loads if load.kind == "reload")
    n_unavailable = sum(
        1 for load in loads if load.cost_basis == "unavailable")
    evidence = document.coverage.skill_load_evidence or {}
    links = document.coverage.dispatch_links or {}
    recognition = document.coverage.phase_recognition or {}
    identity = document.coverage.skill_identity or {}
    text = ("%d loads / %d reloads / %d unavailable / %d redundant bodies "
            "(counted without rows)") % (
                n_loads, n_reloads, n_unavailable,
                evidence.get("redundant_bodies", 0))
    text += (" · dispatches %d / joined via records %d / joined via meta.json "
             "only %d / orphan dispatches %d") % (
                 links.get("dispatches", 0), links.get("joined", 0),
                 links.get("joined_via_meta_only", 0),
                 links.get("orphan_dispatches", 0))
    text += " · phase tiers A/B/C %d/%d/%d" % (
        recognition.get("tierA", 0), recognition.get("tierB", 0),
        recognition.get("tierC", 0))
    if not recognition.get("rule_set_version"):
        text += " / phase rules uncalibrated (rule set empty)"
    if identity.get("merges"):
        pairs = ", ".join(
            "%s ← %s" % (key, ", ".join(identity["aliases"][key]))
            for key in sorted(identity.get("aliases", {})))
        text += " · identity merges %d (%s)" % (identity["merges"], pairs)
    if identity.get("ambiguous"):
        text += " / ambiguous ids stay unmerged: %s" % ", ".join(
            identity["ambiguous"])
    return "<div class='meta'>%s</div>" % esc(text)


def _render_node(node: dict, nodes_by_id: Dict[str, dict],
                 children: Dict[str, List[dict]], orphans: List[dict],
                 path: set, depth: int) -> str:
    if depth >= MAX_DEPTH:
        return ("<div class='notice'>depth limit reached at %s; not expanded"
                "</div>" % esc(node["label"]))
    path = path | {node["agent_id"]}
    ambient = node["ambient"]
    summary = ("%s <span class='badge'>%s</span> "
               "<span class='badge'>%d requests</span>" % (
                   esc(node["label"]), esc(node["model"] or "model ?"),
                   node["n_requests"]))
    if ambient["skills"] > 0:
        summary += (" <span class='badge'>preloaded: %d skills, %d injections, "
                    "~%d EST</span>" % (
                        ambient["skills"], ambient["injections"],
                        ambient["tokens_est"]))
    body = []
    if not node["attachments"] and ambient["skills"] == 0:
        body.append("<p class='empty'>no skill loads, no executions</p>")
    for attachment in node["attachments"]:
        head = esc(attachment["name"])
        if attachment["name"] != attachment["skill_id"]:
            head += " <span class='badge'>%s</span>" % esc(
                attachment["skill_id"])
        for alias in attachment["aliases"]:
            head += " <span class='badge miss'>(alias: %s)</span>" % esc(alias)
        if attachment["executed"]:
            head += " <span class='badge ok'>executed</span>"
        rows = attachment["loads"]
        if rows:
            if attachment["loads_tokens_est"] > 0:
                total = _cost(attachment["loads_tokens_est"])
            else:
                total = "<span class='badge missed'>unavailable</span>"
        else:
            total = "—"
        rows_html = []
        for row in rows:
            kind_class = "ok" if row["kind"] == "load" else "infer"
            rows_html.append(
                "<tr><td><span class='badge %s'>%s</span></td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    kind_class, esc(row["kind"]),
                    esc(row["channel"]) if row["channel"] else "—",
                    esc(fmt_ts(row["ts"])),
                    esc(row["body_sha1"][:8]) if row["body_sha1"] else "—",
                    _cost(row["cost_tokens_est"]),
                ))
        body.append(
            "<details open><summary>%s</summary><div class='body'>"
            "<div class='meta'>total %s</div>%s</details>" % (
                head, total,
                ("<table><tr><th>kind</th><th>channel</th><th>ts</th>"
                 "<th>sha1</th><th>cost</th></tr>%s</table>"
                 % "".join(rows_html)) if rows_html
                else "<p class='empty'>no load rows</p>"))
    for edge in children.get(node["agent_id"], []):
        body.append(
            "<p class='meta'>dispatch · brief %s · %s · %s</p>" % (
                _cost(edge["brief_tokens_est"]),
                esc(edge["subagent_type"] or "?"),
                esc(edge["description"] or "")))
        child = nodes_by_id.get(edge["to_agent_id"])
        if child is None:
            body.append(
                "<p class='empty'>dispatch target %s has no transcript</p>"
                % esc(edge["to_agent_id"]))
            continue
        if child["agent_id"] in path:
            body.append(
                "<p class='notice'>cycle: %s is already on this path; "
                "not expanded</p>" % esc(child["label"]))
            continue
        body.append(_render_node(child, nodes_by_id, children, orphans,
                                 path, depth + 1))
    for edge in orphans:
        if edge["from_agent_id"] == node["agent_id"]:
            body.append(
                "<p class='notice'>orphan dispatch: no matching subagent "
                "transcript (dispatch %s)</p>" % esc(edge["dispatch_id"]))
    return ("<details open><summary>%s</summary><div class='body'>%s</div>"
            "</details>" % (summary, "".join(body)))


def render_skill_tree(document: AuditDocument) -> str:
    tree = skill_tree(document)
    nodes_by_id = {node["agent_id"]: node for node in tree["nodes"]}
    children: Dict[str, List[dict]] = {}
    orphans: List[dict] = []
    for edge in tree["edges"]:
        if edge["to_agent_id"] is None:
            orphans.append(edge)
        else:
            children.setdefault(edge["from_agent_id"], []).append(edge)

    totals = tree["totals"]
    parts = [
        "<!DOCTYPE html>",
        "<html lang='zh'>",
        "<head><meta charset='utf-8'>",
        "<title>Actual Skill Tree</title>",
        "<style>%s</style>" % CSS,
        "</head>",
        "<body>",
        "<header>",
        "<h1>Actual Skill Tree</h1>",
        "<div class='meta'>%s</div>" % " · ".join(
            esc(source) for source in document.source_files),
        "<div class='meta'>"
        "<span class='badge'>IR %s</span>"
        "<span class='badge'>%d agents</span>"
        "<span class='badge'>%d edges</span>"
        "</div>" % (esc(document.ir_version), totals["agents"],
                    totals["edges"]),
        "<div class='meta'>costs are EST (estimated from observed body "
        "tokens); unavailable means no body was observed — a stub-derived "
        "number is never shown.</div>",
        "</header>",
        "<main>",
    ]
    if not tree["nodes"]:
        parts.append("<p class='empty'>no agents in document</p>")
    else:
        if "main" in nodes_by_id:
            roots = [nodes_by_id["main"]]
        else:
            roots = [node for node in tree["nodes"]
                     if node["parent_agent_id"] is None]
        for root in roots:
            parts.append(_render_node(root, nodes_by_id, children, orphans,
                                      set(), 0))
    if tree["loose"]:
        parts.append("<h2>agents without a dispatch edge</h2>")
        for entry in tree["loose"]:
            parts.append("<p>%s <span class='badge'>%s</span></p>" % (
                esc(entry["label"]), esc(entry["reason"])))
    parts.append(_footer(document))
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)
