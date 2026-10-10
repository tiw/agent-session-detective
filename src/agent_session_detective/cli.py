"""CLI entry point: audit an agent session and render the HTML report."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path
from typing import List

from .billing import (
    BillingUnavailable,
    attach_billed_usage,
    compaction_points_from_series,
    query_billed_series,
    session_uuid_from_source,
)
from .catalog import load_catalog
from .ide_db import attach_ide_db
from .if_eval import IFResult, evaluate_playbook
from .ir import build_analyses, build_audit_document
from .judge import Judge, judge_session
from .report import render_report
from .timeline import build_timeline
from .tokenstats import build_token_stats, merge_compaction_windows
from .tree_html import render_skill_tree
from .wire import (
    Session,
    find_latest_codex_session,
    find_latest_qoder_transcript,
    find_latest_session,
    load_session,
)

DEFAULT_SESSIONS_ROOT = "~/.kimi/sessions"
DEFAULT_QODER_PROJECTS_ROOT = "~/.qoder/projects"
DEFAULT_CODEX_SESSIONS_ROOT = "~/.codex/sessions"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="asd",
        description="Audit an agent session against its skills and render an HTML report.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        default=None,
        help="Session source (a Kimi session directory, Qoder .jsonl transcript, "
             "or Codex .jsonl rollout). Defaults to the most recent session.",
    )
    parser.add_argument(
        "--sessions-root",
        default=DEFAULT_SESSIONS_ROOT,
        help="Kimi sessions root used to locate the most recent session (default: %(default)s).",
    )
    parser.add_argument("--skills-dir", action="append", default=None,
                        help="Extra skill directory to include in the catalog (repeatable).")
    parser.add_argument("--out", default=None,
                        help="Report output path (default: <session>/skill-audit.html "
                             "or <transcript>.skill-audit.html).")
    parser.add_argument("--ir-out", default=None, metavar="PATH",
                        help="Write this session's audit IR document as JSON.")
    parser.add_argument("--ir-analyses", default=None, metavar="PATH",
                        help="Write the three IR analyses as JSON (builds the IR in memory).")
    parser.add_argument("--tree-out", default=None, metavar="PATH",
                        help="Render the actual skill tree (agents → dispatches → "
                             "skill loads) as a standalone HTML page.")
    parser.add_argument("--billed-usage", action="store_true",
                        help="Attach provider-billed token totals from the local "
                             "SharedClientCache DB (opt-in; Qoder transcripts only).")
    parser.add_argument("--billed-db", default=None, metavar="PATH",
                        help="Billed-usage DB path (default: Qoder SharedClientCache "
                             "local.db).")
    parser.add_argument("--ide-db", action="store_true",
                        help="Attach IDE-channel subagent chains from the local "
                             "SharedClientCache DB (opt-in; Qoder transcripts only).")
    parser.add_argument("--ide-db-path", default=None, metavar="PATH",
                        help="IDE subagent DB path (default: Qoder SharedClientCache "
                             "local.db).")
    parser.add_argument("--open", action="store_true", help="Open the report in a browser.")
    parser.add_argument("--no-judge", action="store_true", help="Skip LLM trigger judging.")
    parser.add_argument("--expect", default=None,
                        help="Comma-separated skill names expected to be consumed "
                             "(loaded or file-read); rendered as a hit/miss checklist.")
    parser.add_argument("--judge-limit", type=int, default=None,
                        help="Judge only the first N unloaded skills (cheaper runs).")
    parser.add_argument("--steps", action="append", default=None, metavar="PLAYBOOK.md",
                        help="Evaluate instruction following against a playbook's numbered steps "
                             "(repeatable; needs a judge).")
    parser.add_argument("--gate", type=float, default=None,
                        help="CI gate: exit non-zero when IF coverage is below this (default 0.75 "
                             "when --steps is set) or when an --expect skill is missing.")
    parser.add_argument("--serve", action="store_true", help="Run the web app on localhost.")
    parser.add_argument("--port", type=int, default=8471, help="Port for --serve (default 8471).")
    args = parser.parse_args(argv)

    if args.serve:
        from .web import serve
        serve(args.port)
        return 0

    if args.session:
        session_source = Path(args.session).expanduser()
    else:
        kimi_session = find_latest_session(Path(args.sessions_root).expanduser())
        qoder_transcript = find_latest_qoder_transcript(
            Path(DEFAULT_QODER_PROJECTS_ROOT).expanduser()
        )
        codex_session = find_latest_codex_session(
            Path(DEFAULT_CODEX_SESSIONS_ROOT).expanduser()
        )
        candidates = [
            path for path in (kimi_session, qoder_transcript, codex_session)
            if path is not None
        ]
        session_source = max(candidates, key=_session_source_mtime) if candidates else None
    has_log = session_source is not None and (
        (session_source.is_file() and session_source.suffix == ".jsonl")
        or (session_source / "wire.jsonl").exists()
        or (session_source / "agents" / "main" / "wire.jsonl").exists()
    )
    if session_source is None or not has_log:
        print("No session found. Pass a session source or check --sessions-root.", file=sys.stderr)
        return 1

    try:
        session = load_session(session_source)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not session.events:
        print("No events parsed from %s" % session_source, file=sys.stderr)
        return 1
    if args.ide_db:
        attach_ide_db(session, session_source, args.ide_db_path)

    # The document is always built: the report renders the evidence-backed
    # load section from it. --ir-out/--ir-analyses keep serializing it
    # unchanged.
    document = build_audit_document(session, _detect_adapter(session, session_source))
    if args.billed_usage:
        attach_billed_usage(document, session_source, args.billed_db)
    if args.ir_out:
        _write_json(Path(args.ir_out), document.to_dict())
    if args.ir_analyses:
        _write_json(Path(args.ir_analyses), build_analyses(document))
    if args.tree_out:
        tree_path = Path(args.tree_out)
        tree_path.write_text(render_skill_tree(document), encoding="utf-8")
        print(tree_path)

    timeline = build_timeline(session)
    catalog = load_catalog(extra_dirs=args.skills_dir)

    judge = None if args.no_judge else Judge.from_env()
    judgments = []
    if judge:
        unconsumed = max(len(catalog) - len(timeline.consumed_skill_names()), 0)
        n = min(args.judge_limit, unconsumed) if args.judge_limit is not None else unconsumed
        print("Judging %d unconsumed skills (model: %s)..." % (n, judge.model), file=sys.stderr)
        judgments = judge_session(timeline, catalog, judge, limit=args.judge_limit)
    else:
        print("Judge not configured; facts only. See ASD_JUDGE_* env vars.", file=sys.stderr)

    if_results = []
    if args.steps:
        if not judge:
            print("--steps needs a judge (same credentials as trigger judging).", file=sys.stderr)
            return 1
        for playbook in args.steps:
            print("Evaluating instruction following: %s" % playbook, file=sys.stderr)
            if_results.append(
                evaluate_playbook(judge, Path(playbook), session, gate=args.gate)
            )

    billed_points = []
    billed_series = []
    if args.billed_usage:
        billed_uuid = session_uuid_from_source(session_source)
        if billed_uuid is not None:
            try:
                billed_series = query_billed_series(
                    billed_uuid, db_path=args.billed_db)
            except BillingUnavailable as exc:
                document.coverage.notes.append(
                    "billed_compaction: unavailable (%s)" % exc.reason)
            billed_points = compaction_points_from_series(billed_series)
    compaction_windows, compaction_source = merge_compaction_windows(
        timeline, billed_points)
    token_stats = build_token_stats(
        session, timeline,
        compaction_windows=compaction_windows,
        compaction_source=compaction_source,
        billed_series=billed_series)
    if token_stats.usage_records:
        hit = "%.1f%%" % (token_stats.cache_hit_rate * 100) if token_stats.cache_hit_rate is not None else "?"
        print(
            "tokens: input=%d output=%d cache-hit=%s growth=%s"
            % (token_stats.input_total, token_stats.output_total, hit, token_stats.growth_verdict),
            file=sys.stderr,
        )

    report = render_report(
        session, timeline, judgments,
        judge_enabled=judge is not None,
        catalog_size=len(catalog),
        expected=_parse_expected(args.expect),
        if_results=if_results,
        token_stats=token_stats,
        no_self_invoke={s.name.lower() for s in catalog if s.disable_model_invocation},
        document=document,
    )
    out_path = Path(args.out) if args.out else _default_output_path(session_source)
    out_path.write_text(report, encoding="utf-8")
    print(out_path)

    failed = False
    if if_results:
        for r in if_results:
            label = "n/a" if r.not_applicable else "%.2f (gate %.2f)" % (r.coverage, r.gate)
            print("IF %s: %s -> %s" % (r.playbook, label, "PASS" if r.passed else "FAIL"), file=sys.stderr)
            failed = failed or not r.passed
    if args.gate is not None:
        missing = [e for e in _parse_expected(args.expect)
                   if e.lower() not in {n.lower() for n in timeline.consumed_skill_names()}]
        if missing:
            print("MISSING expected skills: %s" % ", ".join(missing), file=sys.stderr)
            failed = True
    if args.open:
        webbrowser.open(out_path.as_uri())
    return 1 if failed else 0


def _write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(path)


def _detect_adapter(session: Session, source: Path) -> str:
    """Best-effort adapter id for the IR document.

    The wire parsers leave distinct fingerprints: only the Qoder parser attaches
    a per-record ``ref``; ``LLMRequest`` events come from the Kimi CLI parser;
    ``TurnTokens``/``SubagentSpawned`` come from the Kimi desktop parser; Codex
    rollouts are ``rollout-*.jsonl`` files. A terminal-CLI Qoder transcript is
    stamped ``source_format`` by load_session and always wins. Anything else
    stays ``unknown``.
    """
    if getattr(session, "source_format", None) == "qoder-cli":
        return "qoder-cli"
    if any(event.ref is not None for event in session.events):
        return "qoder"
    kinds = {event.type for event in session.events}
    if "LLMRequest" in kinds:
        return "kimi-cli"
    if kinds & {"TurnTokens", "SubagentSpawned"}:
        return "kimi-desktop"
    if source.name.startswith("rollout-"):
        return "codex"
    return "unknown"


def _session_source_mtime(source: Path) -> float:
    if source.is_file():
        return source.stat().st_mtime
    wires = [source / "wire.jsonl", source / "agents" / "main" / "wire.jsonl"]
    return max((wire.stat().st_mtime for wire in wires if wire.exists()), default=0.0)


def _default_output_path(source: Path) -> Path:
    if source.is_file():
        return source.with_name(source.stem + ".skill-audit.html")
    return source / "skill-audit.html"


def _parse_expected(raw: str) -> List[str]:
    return [part.strip() for part in raw.split(",") if part.strip()] if raw else []


if __name__ == "__main__":
    sys.exit(main())
