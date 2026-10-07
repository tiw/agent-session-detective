"""CLI entry point: audit a Kimi Code session and render the HTML report."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path
from typing import List

from .catalog import load_catalog
from .if_eval import IFResult, evaluate_playbook
from .judge import Judge, judge_session
from .report import render_report
from .timeline import build_timeline
from .tokenstats import build_token_stats
from .wire import find_latest_qoder_transcript, find_latest_session, load_session

DEFAULT_SESSIONS_ROOT = "~/.kimi/sessions"
DEFAULT_QODER_PROJECTS_ROOT = "~/.qoder/projects"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="asd",
        description="Audit an agent session against its skills and render an HTML report.",
    )
    parser.add_argument(
        "session",
        nargs="?",
        default=None,
        help="Session source (a Kimi session directory or Qoder .jsonl transcript). Defaults to the most recent session.",
    )
    parser.add_argument(
        "--sessions-root",
        default=DEFAULT_SESSIONS_ROOT,
        help="Kimi sessions root used to locate the most recent session (default: %(default)s).",
    )
    parser.add_argument("--skills-dir", action="append", default=None,
                        help="Extra skill directory to include in the catalog (repeatable).")
    parser.add_argument("--out", default=None,
                        help="Report output path (default: <Kimi session>/skill-audit.html or <Qoder transcript>.skill-audit.html).")
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
        candidates = [path for path in (kimi_session, qoder_transcript) if path is not None]
        session_source = max(candidates, key=_session_source_mtime) if candidates else None
    has_log = session_source is not None and (
        (session_source.is_file() and session_source.suffix == ".jsonl")
        or (session_source / "wire.jsonl").exists()
        or (session_source / "agents" / "main" / "wire.jsonl").exists()
    )
    if session_source is None or not has_log:
        print("No session found. Pass a session source or check --sessions-root.", file=sys.stderr)
        return 1

    session = load_session(session_source)
    if not session.events:
        print("No events parsed from %s" % session_source, file=sys.stderr)
        return 1

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

    token_stats = build_token_stats(session, timeline)
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
