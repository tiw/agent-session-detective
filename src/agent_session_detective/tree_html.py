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
:root { --bg:#09090b; --surface:#121214; --line:#26262b; --text:#f4f4f5;
        --dim:#8e8e96; --ok:#3ddc84; --warn:#f5a524; --bad:#f4504c;
        --fact:#58a6ff; }
* { box-sizing:border-box; }
body { font:14px/1.6 ui-monospace,"SF Mono","JetBrains Mono",Menlo,monospace;
       color:var(--text); margin:0; background:var(--bg);
       -webkit-font-smoothing:antialiased; }
header { background:var(--surface); border-bottom:1px solid var(--line);
         padding:16px 24px 14px; }
h1 { margin:0 0 8px; font-size:15px; font-weight:600; letter-spacing:0.08em;
     text-transform:lowercase; color:var(--dim); }
h2 { font-size:12px; text-transform:lowercase; letter-spacing:0.1em;
     color:var(--dim); margin:28px 0 12px; font-weight:600; }
.meta { color:var(--dim); font-size:12.5px; }
main { max-width:1080px; margin:0 auto; padding:24px; }
.badge { display:inline-block; font-size:11.5px; line-height:1.7; padding:1px 8px;
         border-radius:999px; border:1px solid var(--line); color:var(--dim);
         margin-right:6px; white-space:nowrap; }
.badge.fact { color:var(--fact); border-color:rgba(88,166,255,0.4); }
.badge.infer { color:var(--warn); border-color:rgba(245,165,36,0.4); }
.badge.missed { color:var(--bad); border-color:rgba(244,80,76,0.4); }
.badge.ok { color:var(--ok); border-color:rgba(61,220,132,0.4); }
details { background:var(--surface); border:1px solid var(--line);
          border-radius:10px; margin:8px 0; }
summary { cursor:pointer; padding:10px 14px; font-weight:600; }
details[open] > summary { border-bottom:1px solid var(--line); }
.body { padding:12px 14px; }
pre { background:var(--bg); border:1px solid var(--line); border-radius:8px;
      padding:10px 12px; overflow-x:auto; font-size:12px; line-height:1.55;
      white-space:pre-wrap; word-break:break-all; }
blockquote { margin:6px 0; padding:4px 12px; border-left:2px solid var(--fact);
             color:var(--dim); font-size:12.5px; }
table { border-collapse:collapse; width:100%; font-size:12.5px; }
td,th { border-bottom:1px solid var(--line); padding:6px 10px; text-align:left;
        vertical-align:top; }
th { color:var(--dim); font-weight:500; font-size:11.5px; }
tr:last-child td { border-bottom:none; }
.notice { padding:10px 14px; border-radius:8px; background:rgba(245,165,36,0.06);
          border:1px solid rgba(245,165,36,0.4); color:var(--warn); }
.empty { color:var(--dim); font-style:italic; }
.sources summary { font-weight:400; color:var(--dim); font-size:12.5px; }
.sources ul { margin:8px 0; padding:2px 14px 10px; list-style:none;
              max-height:280px; overflow-y:auto; }
.sources li { padding:2px 0; font-size:12px; color:var(--dim);
              word-break:break-all; }
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
            head += " <span class='badge missed'>(alias: %s)</span>" % esc(alias)
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
            "<div class='meta'>total %s</div>%s</div></details>" % (
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
        "<details class='sources'><summary>%d source transcript%s</summary>"
        "<ul>%s</ul></details>" % (
            len(document.source_files),
            "" if len(document.source_files) == 1 else "s",
            "".join("<li>%s</li>" % esc(source)
                    for source in document.source_files)),
        "<div class='meta'>"
        "<span class='badge'>IR %s</span>"
        "<span class='badge'>%d agents</span>"
        "<span class='badge'>%d edges</span>"
        "</div>" % (esc(document.ir_version), totals["agents"],
                    totals["edges"]),
    ]
    if document.adapter.get("id") == "qoder-cli":
        parts.append(
            "<div class='notice'>degraded source: this transcript carries "
            "no usage telemetry, so token costs are unavailable — tool "
            "calls and dispatch edges still reconstruct.</div>")
    parts.extend([
        "<div class='meta'>costs are EST (estimated from observed body "
        "tokens); unavailable means no body was observed — a stub-derived "
        "number is never shown.</div>",
        "</header>",
        "<main>",
    ])
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
            parts.append(_render_node(nodes_by_id[entry["agent_id"]],
                                      nodes_by_id, children, orphans,
                                      set(), 0))
    parts.append(_footer(document))
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)
