# src/agent_session_detective/catalog_audit.py
"""Static health check of the skill catalog.

Four questions, all answerable offline:

1. **size**      — what does activating this skill cost?
2. **description** — is the routing wording usable?
3. **layering**  — is progressive disclosure paying for itself?
4. **parsing**   — will this skill be audited on sound routing evidence?

Everything here is a *fact*, not a judgment. The parser is ours, so a
description that did not survive it is a defect we can assert rather than a
risk we can only warn about. Nothing in this module says whether an agent did
a good job — that is ``asd <session>``'s job, and the two reports stay apart
even though they share the catalog.

Token counts come from ``ir.estimator``, which wraps the project's single
versioned estimator. Importing ``timeline.estimate_tokens`` directly would
bypass ``ESTIMATOR_VERSION`` and fork the definition of a token.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .catalog import Skill, parse_failures
from .ir.estimator import ESTIMATOR_VERSION, estimate

# Conventional thresholds, not a specification. They come from a report that
# eyeballed 68 SKILL.md files; nothing about the format makes 500 the right
# line. Every one is overridable, and the report labels them as conventions so
# a reader does not mistake them for rules.
DEFAULT_MAX_LINES = 500
DEFAULT_LARGE_LINES = 200
DEFAULT_MIN_DESC_CHARS = 50
DEFAULT_MAX_DESC_CHARS = 300


@dataclass
class SizeEntry:
    """One skill's activation cost."""

    name: str
    lines: int
    tokens: int
    tier: str  # "oversize" | "large" | "ok"
    source: str
    has_body: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lines": self.lines,
            "tokens": self.tokens,
            "tier": self.tier,
            "source": self.source,
            "has_body": self.has_body,
        }


@dataclass
class DescEntry:
    """One skill's routing-wording cost."""

    name: str
    chars: int
    tokens: int
    tier: str  # "verbose" | "terse" | "ok" | "unusable"
    source: str
    sample: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "chars": self.chars,
            "tokens": self.tokens,
            "tier": self.tier,
            "source": self.source,
            "sample": self.sample,
        }


@dataclass
class CatalogAudit:
    """The four blocks, plus what could not be measured."""

    total: int
    estimator: str
    thresholds: Dict[str, int]

    size_tiers: Dict[str, int]
    size: List[SizeEntry]

    desc_tiers: Dict[str, int]
    descriptions: List[DescEntry]

    discovery_tokens: int
    activation_tokens: int
    ratio: Optional[float]
    body_count: int
    no_body: List[str]

    parse_failures: List[dict]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "estimator": self.estimator,
            "thresholds": self.thresholds,
            "size_tiers": self.size_tiers,
            "size": [e.to_dict() for e in self.size],
            "desc_tiers": self.desc_tiers,
            "descriptions": [e.to_dict() for e in self.descriptions],
            "discovery_tokens": self.discovery_tokens,
            "activation_tokens": self.activation_tokens,
            "ratio": self.ratio,
            "body_count": self.body_count,
            "no_body": self.no_body,
            "parse_failures": self.parse_failures,
        }


def read_body(skill: Skill) -> Optional[str]:
    """The skill's own file, or ``None`` when there is nothing to read.

    An injected entry with no ``body_path`` carries the sentinel path
    ``<injected>``, and ``body_path`` pointing at a missing file raises on
    read. Both mean the same thing here: this skill has no body on disk, so it
    has no activation cost to measure. Returning ``None`` keeps the caller
    from inventing a zero — a missing body is not a small skill.
    """
    try:
        return Path(skill.path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _size_tier(lines: int, max_lines: int, large_lines: int) -> str:
    if lines > max_lines:
        return "oversize"
    if lines > large_lines:
        return "large"
    return "ok"


def _desc_tier(chars: int, skill: Skill, min_chars: int, max_chars: int) -> str:
    if skill.description_parse_failed:
        return "unusable"
    if chars > max_chars:
        return "verbose"
    if chars < min_chars:
        return "terse"
    return "ok"


def audit_catalog(
    skills: List[Skill],
    max_lines: int = DEFAULT_MAX_LINES,
    large_lines: int = DEFAULT_LARGE_LINES,
    min_desc_chars: int = DEFAULT_MIN_DESC_CHARS,
    max_desc_chars: int = DEFAULT_MAX_DESC_CHARS,
) -> CatalogAudit:
    """Measure the catalog. No session, no LLM, no network."""
    size: List[SizeEntry] = []
    descriptions: List[DescEntry] = []
    no_body: List[str] = []
    discovery = 0
    activation = 0
    body_count = 0

    for skill in skills:
        # Discovery cost is the description alone: it is what stays resident
        # in every request, whether or not the skill ever loads.
        desc_tokens = estimate(skill.description)
        discovery += desc_tokens
        descriptions.append(
            DescEntry(
                name=skill.name,
                chars=len(skill.description),
                tokens=desc_tokens,
                tier=_desc_tier(
                    len(skill.description), skill, min_desc_chars, max_desc_chars),
                source=skill.source,
                sample=skill.description[:160],
            )
        )

        text = read_body(skill)
        if text is None:
            no_body.append(skill.name)
            continue
        body_count += 1
        tokens = estimate(text)
        activation += tokens
        lines = len(text.splitlines())
        size.append(
            SizeEntry(
                name=skill.name,
                lines=lines,
                tokens=tokens,
                tier=_size_tier(lines, max_lines, large_lines),
                source=skill.source,
                has_body=True,
            )
        )

    size.sort(key=lambda e: e.tokens, reverse=True)
    descriptions.sort(key=lambda e: e.chars, reverse=True)

    size_tiers = {"oversize": 0, "large": 0, "ok": 0}
    for entry in size:
        size_tiers[entry.tier] += 1

    desc_tiers = {"verbose": 0, "terse": 0, "ok": 0, "unusable": 0}
    for entry in descriptions:
        desc_tiers[entry.tier] += 1

    return CatalogAudit(
        total=len(skills),
        estimator=ESTIMATOR_VERSION,
        thresholds={
            "max_lines": max_lines,
            "large_lines": large_lines,
            "min_desc_chars": min_desc_chars,
            "max_desc_chars": max_desc_chars,
        },
        size_tiers=size_tiers,
        size=size,
        desc_tiers=desc_tiers,
        descriptions=descriptions,
        discovery_tokens=discovery,
        activation_tokens=activation,
        ratio=(activation / discovery) if discovery else None,
        body_count=body_count,
        no_body=sorted(no_body),
        parse_failures=[
            {"name": s.name, "source": s.source, "sample": s.description[:80]}
            for s in parse_failures(skills)
        ],
    )


TIER_LABELS = {
    "oversize": "超标",
    "large": "偏大",
    "ok": "OK",
    "verbose": "冗余",
    "terse": "过短",
    "unusable": "解析失败",
}


def render_audit(audit: CatalogAudit, top: int = 10) -> str:
    """Plain-text report. Deterministic ordering, no color, no judgment."""
    lines: List[str] = []
    add = lines.append

    add("catalog: %d skills (token 估算器 %s)" % (audit.total, audit.estimator))
    add("")

    # 1. layering first: it is the answer to "is this mechanism worth it".
    add("分层有效性")
    if audit.ratio is None:
        add("  discovery 常驻 0 tokens —— 没有可用的 description，无法计算比值")
    else:
        add("  discovery 常驻     ~%s tokens  (Σ description，始终常驻)"
            % _fmt(audit.discovery_tokens))
        add("  activation 潜在    ~%s tokens  (Σ 整份 SKILL.md，触发才付)"
            % _fmt(audit.activation_tokens))
        add("  比值 %.1f:1  —— 比值越小，分层越有效" % audit.ratio)
    if audit.no_body:
        add("  %d 个 skill 无正文（多为注入且未提供 body_path），未计入 activation"
            % len(audit.no_body))
    add("")

    # 2. parsing health: a defect here silently corrupts every other verdict.
    add("解析健康度")
    if not audit.parse_failures:
        add("  ✓ 全部 description 均可解析")
    else:
        add("  ✗ %d 个 description 解析失败 —— 审计时缺少路由依据"
            % len(audit.parse_failures))
        for entry in audit.parse_failures:
            add("      %-28s %r" % (entry["name"], entry["sample"]))
    add("")

    # 3. size.
    add("体量分级（约定阈值：>%d 行超标 / %d-%d 偏大，按 token 排序）"
        % (audit.thresholds["max_lines"], audit.thresholds["large_lines"] + 1,
           audit.thresholds["max_lines"]))
    add("  超标 %d 个 · 偏大 %d 个 · OK %d 个（共 %d 个有正文）"
        % (audit.size_tiers["oversize"], audit.size_tiers["large"],
           audit.size_tiers["ok"], audit.body_count))
    for entry in audit.size[:top]:
        add("  %5d 行  ~%-8s %s" % (entry.lines, _fmt(entry.tokens), entry.name))
    if len(audit.size) > top:
        add("  …… 另有 %d 个（用 --top 0 显示全部）" % (len(audit.size) - top))
    add("")

    # 4. description quality.
    add("description 质量（约定阈值：%d-%d 字符为佳）"
        % (audit.thresholds["min_desc_chars"], audit.thresholds["max_desc_chars"]))
    add("  冗余 >%d 字符 %d 个 · 过短 <%d 字符 %d 个 · 解析失败 %d 个 · 合格 %d 个"
        % (audit.thresholds["max_desc_chars"], audit.desc_tiers["verbose"],
           audit.thresholds["min_desc_chars"], audit.desc_tiers["terse"],
           audit.desc_tiers["unusable"], audit.desc_tiers["ok"]))
    flagged = descriptions_of_interest(audit, top=max(top, 0))
    for entry in flagged:
        add("  %4d 字符  %-8s %s" % (entry.chars, TIER_LABELS[entry.tier], entry.name))
    remaining = sum(
        1 for e in audit.descriptions if e.tier != "ok") - len(flagged)
    if remaining > 0:
        add("  …… 另有 %d 个（用 --top 0 显示全部）" % remaining)
    add("")
    add("阈值是约定值，不是规范要求；用 --max-lines / --min-desc / --max-desc 覆盖。")

    return "\n".join(lines)


def descriptions_of_interest(audit: CatalogAudit, top: int = 10) -> List[DescEntry]:
    """The descriptions worth looking at: unusable, then verbose, then terse.

    Ordered worst-first rather than longest-first: a 40-character description
    that cannot route is a bigger problem than a 320-character one that can.
    """
    order = {"unusable": 0, "verbose": 1, "terse": 2, "ok": 3}
    flagged = [e for e in audit.descriptions if e.tier != "ok"]
    flagged.sort(key=lambda e: (order[e.tier], e.chars))
    return flagged[:top] if top else flagged


def _fmt(n: int) -> str:
    if n >= 10000:
        return "%.1fk" % (n / 1000.0)
    return str(n)
