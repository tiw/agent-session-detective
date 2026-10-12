# src/agent_session_detective/catalog_audit.py
"""Static health check of the skill catalog.

Six questions, all answerable offline:

1. **size**      — what does activating this skill cost?
2. **description** — is the routing wording usable?
3. **layering**  — is progressive disclosure paying for itself?
4. **parsing**   — will this skill be audited on sound routing evidence?
5. **duplication** — are near-copies hiding between entries?
6. **closure**   — what does one activation drag in?

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

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .catalog import Skill, _body_of, parse_failures
from .ir.estimator import ESTIMATOR_VERSION, estimate

# Conventional thresholds, not a specification. They come from a report that
# eyeballed 68 SKILL.md files; nothing about the format makes 500 the right
# line. Every one is overridable, and the report labels them as conventions so
# a reader does not mistake them for rules.
DEFAULT_MAX_LINES = 500
DEFAULT_LARGE_LINES = 200
DEFAULT_MIN_DESC_CHARS = 50
DEFAULT_MAX_DESC_CHARS = 300

# Duplication conventions. Bodies compare on line-window shingles and flag on
# containment: a copy of a section is asymmetric (every shingle of the copy
# exists in the original), which jaccard would dilute. Descriptions compare on
# word-window shingles and flag on jaccard: two routing texts that half
# overlap are a listing collision, and that relation is symmetric.
DEFAULT_SHINGLE_K = 3
DEFAULT_DUP_CONTAINMENT = 0.6
DEFAULT_DESC_DUP_CONTAINMENT = 0.8

# Closure conventions, in the same spirit as the ones above: a depth cap so a
# pathological requires chain cannot walk forever (and the stop is disclosed),
# and a warn line where one activation's tree stops being cheap.
DEFAULT_CLOSURE_DEPTH = 4
DEFAULT_CLOSURE_WARN_TOKENS = 10000


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
class DupPair:
    """One near-duplicate pair: a body copy or a routing-text collision.

    ``waste_tokens_est`` is the smaller side — activation tokens for a body
    pair, resident (discovery) tokens for a desc pair — i.e. what merging the
    pair would save at that layer.
    """

    a: str
    b: str
    kind: str  # "body" | "desc"
    containment: float
    jaccard: float
    exact: bool
    waste_tokens_est: int

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "kind": self.kind,
            "containment": self.containment,
            "jaccard": self.jaccard,
            "exact": self.exact,
            "waste_tokens_est": self.waste_tokens_est,
        }


@dataclass
class ClosureEntry:
    """One skill's static loading closure.

    ``skills`` are the reachable requires targets (root excluded, sorted);
    ``files`` are referenced local files that exist on disk, namespaced
    ``"skill:path"`` when the owner is not the root; ``tokens`` is the whole
    tree — root body, dependency bodies, referenced files — counted the way
    the size section counts bodies (whole file, frontmatter included).
    ``truncated`` means the depth cap stopped expansion with requires still
    unexpanded.
    """

    name: str
    skills: List[str]
    files: List[str]
    tokens: int
    unresolved: List[str]
    truncated: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "skills": self.skills,
            "files": self.files,
            "tokens": self.tokens,
            "unresolved": self.unresolved,
            "truncated": self.truncated,
        }


@dataclass
class CatalogAudit:
    """The measured blocks, plus what could not be measured."""

    total: int
    estimator: str
    thresholds: Dict[str, float]

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

    duplicates: List[DupPair]
    dup_clusters: List[List[str]]
    dup_rate: Optional[float]
    dup_waste_activation_tokens: int
    dup_waste_discovery_tokens: int

    closures: List[ClosureEntry]

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
            "duplicates": [e.to_dict() for e in self.duplicates],
            "dup_clusters": self.dup_clusters,
            "dup_rate": self.dup_rate,
            "dup_waste_activation_tokens": self.dup_waste_activation_tokens,
            "dup_waste_discovery_tokens": self.dup_waste_discovery_tokens,
            "closures": [e.to_dict() for e in self.closures],
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


_MD_NOISE_RE = re.compile(r"^(?:#{1,6}\s+|[-*+]\s+)")
_WORD_RE = re.compile(r"\w+")


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _norm_lines(text: str) -> List[str]:
    """Body text as comparable lines: markdown noise off, blanks dropped."""
    lines: List[str] = []
    for raw in text.splitlines():
        line = " ".join(_MD_NOISE_RE.sub("", raw.strip()).split())
        if line:
            lines.append(line)
    return lines


def _line_shingles(lines: List[str], k: int) -> set:
    if len(lines) < k:
        # Shorter than one window still gets an identity; skipping it would
        # exempt exactly the small skills most likely to be near-copies.
        return {_digest(line) for line in lines}
    return {_digest("\n".join(lines[i:i + k]))
            for i in range(len(lines) - k + 1)}


def _word_shingles(text: str, k: int) -> set:
    words = _WORD_RE.findall(text.lower())
    if len(words) < k:
        return {_digest(word) for word in words}
    return {_digest(" ".join(words[i:i + k]))
            for i in range(len(words) - k + 1)}


def _similarity(a: set, b: set) -> Tuple[float, float]:
    """(containment, jaccard). Empty sides score zero, never a division."""
    if not a or not b:
        return 0.0, 0.0
    shared = len(a & b)
    return shared / min(len(a), len(b)), shared / len(a | b)


def _clusters(pairs: List[DupPair]) -> List[List[str]]:
    """Union-find over body pairs: chains of copies merge into one candidate."""
    parent: Dict[str, str] = {}

    def find(name: str) -> str:
        parent.setdefault(name, name)
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != root:
            parent[name], name = root, parent[name]
        return root

    for pair in pairs:
        if pair.kind != "body":
            continue
        root_a, root_b = find(pair.a), find(pair.b)
        if root_a != root_b:
            parent[root_b] = root_a

    groups: Dict[str, List[str]] = {}
    for name in parent:
        groups.setdefault(find(name), []).append(name)
    return sorted(sorted(members) for members in groups.values())


def detect_duplicates(
    skills: List[Skill],
    containment: float = DEFAULT_DUP_CONTAINMENT,
    desc_containment: float = DEFAULT_DESC_DUP_CONTAINMENT,
    shingle_k: int = DEFAULT_SHINGLE_K,
) -> List[DupPair]:
    """Near-duplicate pairs across the catalog, worst first.

    Body pairs flag on containment — a copied section is asymmetric (every
    shingle of the copy lives in the original), which jaccard would dilute.
    Desc pairs flag on jaccard — two routing texts that half overlap are a
    symmetric listing collision. ``waste_tokens_est`` is the smaller side:
    what merging the pair would save at that layer (activation for bodies,
    resident discovery text for descriptions).
    """
    bodies: List[Tuple[str, List[str], set, int]] = []
    descs: List[Tuple[str, str, set, int]] = []
    for skill in skills:
        text = read_body(skill)
        if text is not None:
            lines = _norm_lines(_body_of(text))
            if lines:
                bodies.append((skill.name, lines,
                               _line_shingles(lines, shingle_k), estimate(text)))
        norm = " ".join(skill.description.split())
        if norm:
            descs.append((skill.name, norm, _word_shingles(norm, shingle_k),
                          estimate(skill.description)))

    pairs: List[DupPair] = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            name_a, lines_a, shingles_a, tokens_a = bodies[i]
            name_b, lines_b, shingles_b, tokens_b = bodies[j]
            pair_containment, pair_jaccard = _similarity(shingles_a, shingles_b)
            if pair_containment < containment:
                continue
            pairs.append(DupPair(
                a=name_a, b=name_b, kind="body",
                containment=pair_containment, jaccard=pair_jaccard,
                exact=lines_a == lines_b,
                waste_tokens_est=min(tokens_a, tokens_b),
            ))
    for i in range(len(descs)):
        for j in range(i + 1, len(descs)):
            name_a, norm_a, shingles_a, tokens_a = descs[i]
            name_b, norm_b, shingles_b, tokens_b = descs[j]
            pair_containment, pair_jaccard = _similarity(shingles_a, shingles_b)
            if pair_jaccard < desc_containment:
                continue
            pairs.append(DupPair(
                a=name_a, b=name_b, kind="desc",
                containment=pair_containment, jaccard=pair_jaccard,
                exact=norm_a == norm_b,
                waste_tokens_est=min(tokens_a, tokens_b),
            ))
    pairs.sort(key=lambda pair: (pair.kind, -pair.containment,
                                 -pair.jaccard, pair.a, pair.b))
    return pairs


_MD_LINK_RE = re.compile(r"\]\(\s*([^)\s]+)\s*\)")
_LOCAL_DIR_RE = re.compile(
    r"(?<![\w/-])((?:references|scripts|assets|templates)/[\w.\-]+(?:/[\w.\-]+)*)")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


def local_references(body: str) -> List[str]:
    """Relative paths a body points at: markdown links and bare dir mentions.

    Scheme URLs and pure anchors are not local reads. Fragments and queries
    are dropped; the result is deduplicated and sorted so two audits of one
    file agree.
    """
    found: set = set()
    for match in _MD_LINK_RE.finditer(body):
        found.add(match.group(1))
    for match in _LOCAL_DIR_RE.finditer(body):
        found.add(match.group(1))
    out: List[str] = []
    for raw in sorted(found):
        target = raw.split("#", 1)[0].split("?", 1)[0]
        if not target or _SCHEME_RE.match(target):
            continue
        out.append(target)
    return out


def detect_closures(
    skills: List[Skill], depth: int = DEFAULT_CLOSURE_DEPTH
) -> List[ClosureEntry]:
    """Every scannable skill's loading closure, most expensive first.

    A skill with no readable body (injected without ``body_path``) cannot be
    scanned and is not a root; its absence stays visible via ``no_body``.
    """
    by_name = {skill.name: skill for skill in skills}
    entries: List[ClosureEntry] = []
    for root in skills:
        root_text = read_body(root)
        if root_text is None:
            continue
        entries.append(_closure_entry(root, root_text, by_name, depth))
    entries.sort(key=lambda entry: (-entry.tokens, entry.name))
    return entries


def _closure_entry(
    root: Skill, root_text: str, by_name: Dict[str, Skill], depth: int
) -> ClosureEntry:
    tokens = estimate(root_text)
    visited = {root.name}
    file_costs: Dict[Path, str] = {}
    unresolved: List[str] = []
    truncated = False

    def scan(skill: Skill, text: str) -> None:
        nonlocal tokens
        base = Path(skill.path).parent
        for rel in local_references(_body_of(text)):
            resolved = Path(os.path.normpath(str(base / rel)))
            if resolved in file_costs:
                continue
            try:
                if not resolved.is_file():
                    continue
                file_text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tokens += estimate(file_text)
            file_costs[resolved] = (rel if skill is root
                                    else "%s:%s" % (skill.name, rel))

    queue: List[Tuple[Skill, str, int]] = [(root, root_text, 0)]
    while queue:
        skill, text, level = queue.pop(0)
        scan(skill, text)
        if level >= depth:
            if skill.requires:
                truncated = True
            continue
        for name in skill.requires:
            if name in visited:
                continue
            dependency = by_name.get(name)
            if dependency is None:
                if name not in unresolved:
                    unresolved.append(name)
                continue
            visited.add(name)
            dep_text = read_body(dependency)
            if dep_text is None:
                queue.append((dependency, "", level + 1))
            else:
                tokens += estimate(dep_text)
                queue.append((dependency, dep_text, level + 1))

    return ClosureEntry(
        name=root.name,
        skills=sorted(visited - {root.name}),
        files=sorted(file_costs.values()),
        tokens=tokens,
        unresolved=sorted(unresolved),
        truncated=truncated,
    )


def audit_catalog(
    skills: List[Skill],
    max_lines: int = DEFAULT_MAX_LINES,
    large_lines: int = DEFAULT_LARGE_LINES,
    min_desc_chars: int = DEFAULT_MIN_DESC_CHARS,
    max_desc_chars: int = DEFAULT_MAX_DESC_CHARS,
    dup_containment: float = DEFAULT_DUP_CONTAINMENT,
    desc_dup_containment: float = DEFAULT_DESC_DUP_CONTAINMENT,
    shingle_k: int = DEFAULT_SHINGLE_K,
    closure_depth: int = DEFAULT_CLOSURE_DEPTH,
    closure_warn_tokens: int = DEFAULT_CLOSURE_WARN_TOKENS,
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

    duplicates = detect_duplicates(
        skills,
        containment=dup_containment,
        desc_containment=desc_dup_containment,
        shingle_k=shingle_k,
    )
    dup_clusters = _clusters(duplicates)
    involved = {name for cluster in dup_clusters for name in cluster}
    dup_waste_activation = sum(
        duplicate.waste_tokens_est
        for duplicate in duplicates if duplicate.kind == "body")
    dup_waste_discovery = sum(
        duplicate.waste_tokens_est
        for duplicate in duplicates if duplicate.kind == "desc")

    closures = detect_closures(skills, depth=closure_depth)

    return CatalogAudit(
        total=len(skills),
        estimator=ESTIMATOR_VERSION,
        thresholds={
            "max_lines": max_lines,
            "large_lines": large_lines,
            "min_desc_chars": min_desc_chars,
            "max_desc_chars": max_desc_chars,
            "dup_containment": dup_containment,
            "desc_dup_containment": desc_dup_containment,
            "shingle_k": shingle_k,
            "closure_depth": closure_depth,
            "closure_warn_tokens": closure_warn_tokens,
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
        duplicates=duplicates,
        dup_clusters=dup_clusters,
        dup_rate=(len(involved) / len(skills)) if skills else None,
        dup_waste_activation_tokens=dup_waste_activation,
        dup_waste_discovery_tokens=dup_waste_discovery,
        closures=closures,
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

    # 5. duplication: what is hiding between entries. Body copies and listing
    # collisions cost different layers, so the two counts never merge.
    add("重复度（约定阈值：body containment ≥ %.2f · desc jaccard ≥ %.2f · %d 重窗）"
        % (audit.thresholds["dup_containment"],
           audit.thresholds["desc_dup_containment"],
           int(audit.thresholds["shingle_k"])))
    if not audit.duplicates:
        add("  ✓ 未发现近重复")
    else:
        body_pairs = sum(1 for p in audit.duplicates if p.kind == "body")
        desc_pairs = len(audit.duplicates) - body_pairs
        rate = ("%.0f%%" % (audit.dup_rate * 100)
                if audit.dup_rate is not None else "n/a")
        add("  body 近重复 %d 对 · desc 近重复 %d 对 · body 涉及占比 %s"
            % (body_pairs, desc_pairs, rate))
        shown = audit.duplicates[:top] if top else audit.duplicates
        for pair in shown:
            layer = "激活" if pair.kind == "body" else "常驻"
            exact = " · 完全相同" if pair.exact else ""
            add("  [%s] %s ↔ %s  containment %.2f · jaccard %.2f%s"
                % (pair.kind, pair.a, pair.b,
                   pair.containment, pair.jaccard, exact))
            add("        合并可省 ~%s tokens（%s口径，取较小一侧）"
                % (_fmt(pair.waste_tokens_est), layer))
        if top and len(audit.duplicates) > top:
            add("  …… 另有 %d 对（用 --top 0 显示全部）"
                % (len(audit.duplicates) - top))
    add("")

    # 6. closure: what one activation drags in, statically.
    warn = audit.thresholds["closure_warn_tokens"]
    depth = int(audit.thresholds["closure_depth"])
    add("加载闭包（约定阈值：>%s tokens 偏重 · requires 深度上限 %d 跳）"
        % (_fmt(warn), depth))
    heavy = sum(1 for entry in audit.closures if entry.tokens > warn)
    with_deps = sum(
        1 for entry in audit.closures if entry.skills or entry.files)
    add("  偏重 %d 个 · 带依赖 %d 个 · 可扫描 %d 个（无正文 %d 个未计入）"
        % (heavy, with_deps, len(audit.closures), len(audit.no_body)))
    shown = audit.closures[:top] if top else audit.closures
    for entry in shown:
        parts = []
        if entry.skills:
            parts.append("→ " + ", ".join(entry.skills))
        if entry.files:
            parts.append("· " + ", ".join(entry.files))
        tail = ("  " + " ".join(parts)) if parts else ""
        mark = " ⚠" if entry.tokens > warn else ""
        add("  ~%-8s %s%s%s" % (_fmt(entry.tokens), entry.name, tail, mark))
        if entry.unresolved:
            add("        未解析的 requires：%s" % ", ".join(entry.unresolved))
        if entry.truncated:
            add("        深度截断：%d 跳处仍有 requires 未展开" % depth)
    if top and len(audit.closures) > top:
        add("  …… 另有 %d 个（用 --top 0 显示全部）"
            % (len(audit.closures) - top))
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
