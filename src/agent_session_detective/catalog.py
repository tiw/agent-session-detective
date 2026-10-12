"""Load the skill catalog from local skill directories.

Each skill is a directory containing SKILL.md: optional YAML frontmatter
(name, description) followed by markdown instructions. The description plus
the opening of the body is what an agent sees when deciding whether to load
the skill, so that is what we audit against.

A catalog entry may also arrive from the outside — a person supplying the
real text of a skill the audit should reason about. Those entries are marked
``source="injected"`` and keep their origin visible all the way to the
report: an injected description is external input, never a log fact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SKILL_DIRS = ("~/.agents/skills", "~/.kimi/skills")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# A description this short did not survive parsing: the frontmatter was
# absent, or it carried no description at all. A block scalar
# (``description: >-``) is *not* a parse failure — that shape is understood.
MIN_MEANINGFUL_DESCRIPTION = 3

# How much of the body follows the description as routing context.
TRIGGER_BODY_CHARS = 800

# A user-supplied directory may nest skills several levels down — plugin
# caches typically put them at <vendor>/<plugin>/<version>/SKILL.md. The
# default dirs are flat, so they stay on the one-level glob; only explicitly
# supplied directories are walked deeper.
MAX_SKILL_DEPTH = 5

# A block scalar header: "|" or ">" plus an optional indent/chomping indicator
# in either order ("|", ">-", "|+", "|2", "|2-", ">-2", ...).
_BLOCK_SCALAR_RE = re.compile(r"[|>][0-9+\-]{0,2}")

# "key:" or "key: value" on one line. The key may not itself contain ":", so a
# bare URL is not mistaken for a mapping entry.
_KEY_RE = re.compile(r"^(?P<key>[^\s:#][^:]*?)\s*:(?:\s+(?P<value>.*))?$")


@dataclass
class Skill:
    name: str
    description: str
    trigger_excerpt: str  # what the agent sees when deciding to trigger
    path: Path
    # frontmatter disable-model-invocation: true means the model may not load
    # this skill on its own — only the router (or the user) may name it.
    disable_model_invocation: bool = False
    # "catalog" = scanned from a skill directory; "injected" = supplied from
    # the outside for this audit. Rendered distinctly so a reader never
    # mistakes supplied text for something the log recorded.
    source: str = "catalog"
    # A person's statement of what the skill is for. It is judgment, not
    # fact: it may reach the judge's prompt but never a factual report block.
    intent: Optional[str] = None
    # True when the description could not be parsed out of the frontmatter.
    description_parse_failed: bool = False
    # Other skills this one pulls in, by name (frontmatter ``requires:``).
    # The static closure walks these edges; it never assumes they loaded.
    requires: List[str] = field(default_factory=list)

    @property
    def is_injected(self) -> bool:
        return self.source == "injected"


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_key_line(text: str) -> bool:
    """True if this line opens a mapping key rather than continuing a scalar."""
    return bool(_KEY_RE.match(text.strip()))


def _strip_comment(text: str) -> str:
    """Drop a trailing YAML comment.

    Per the YAML spec a comment starts at " #" — a space then a hash. A hash
    glued to the preceding word ("keep#tag", a URL fragment) is content.
    Matched against PyYAML on real frontmatter.
    """
    index = text.find(" #")
    return text[:index].rstrip() if index >= 0 else text


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _scalar(text: str) -> str:
    """A single-line scalar: strip a trailing comment, then unwrap quotes."""
    text = text.strip()
    if text[:1] not in ('"', "'"):
        text = _strip_comment(text)
    return _unquote(text)


def _fold(lines: List[str]) -> str:
    """Join wrapped lines the way YAML does.

    One break between two non-empty lines becomes a space; a blank line
    becomes a newline. This is what the agent's own YAML loader produces, so
    the audit must judge the same text the agent actually saw.
    """
    out = ""
    started = False
    pending_blanks = 0
    for line in lines:
        if not line.strip():
            if started:
                pending_blanks += 1
            continue
        if not started:
            out = line.strip()
            started = True
        elif pending_blanks:
            out += "\n" * pending_blanks + line.strip()
        else:
            out += " " + line.strip()
        pending_blanks = 0
    return out


def _block_extent(lines: List[str], pos: int, parent_indent: int) -> Tuple[int, Optional[int]]:
    """Span of the indented block owned by a key: (end, first content indent).

    Blank lines belong to the block — inside a block scalar they are real
    newlines. The block ends at the first non-blank line indented no deeper
    than its owning key.
    """
    end = pos
    content_indent: Optional[int] = None
    while end < len(lines):
        line = lines[end]
        if not line.strip():
            end += 1
            continue
        indent = _indent_of(line)
        if indent <= parent_indent:
            break
        if content_indent is None:
            content_indent = indent
        end += 1
    return end, content_indent


def _dedent(line: str, width: int) -> str:
    return line[width:] if len(line) > width else ""


def _read_block_scalar(
    lines: List[str], pos: int, parent_indent: int, header: str
) -> Tuple[str, int]:
    """Read a "|" / ">" block scalar. Returns (value, next line index)."""
    folded = header[0] == ">"
    digits = "".join(c for c in header[1:] if c.isdigit())
    chomp = "".join(c for c in header[1:] if c in "+-")
    end, auto_indent = _block_extent(lines, pos, parent_indent)
    content_indent = (
        parent_indent + int(digits) if digits else (auto_indent or parent_indent + 2)
    )
    body = ["" if not line.strip() else _dedent(line, content_indent) for line in lines[pos:end]]
    value = _fold(body) if folded else "\n".join(body)
    if chomp == "-":
        value = value.rstrip("\n")
    elif chomp != "+":
        value = value.rstrip("\n") + "\n" if value.strip() else ""
    # Only newlines go: leading spaces inside a literal block are content
    # (an explicit "|"2" indent indicator makes them meaningful), so a full
    # strip() here would silently rewrite the scalar.
    return value.strip("\n"), end


def _read_nested(lines: List[str], pos: int, parent_indent: int) -> Tuple[Any, int]:
    """Read the indented block under a key that had no inline value.

    It is a nested mapping or sequence when the block's first meaningful line
    looks like ``key:`` or ``- item``. Otherwise it is a plain multi-line
    scalar — the ``description:`` followed by indented prose shape — folded
    into a single string.
    """
    end, _ = _block_extent(lines, pos, parent_indent)
    if end == pos:
        return "", pos
    first = next(line for line in lines[pos:end] if line.strip())
    child_indent = _indent_of(first)
    body = first.strip()
    if body.startswith("- ") or body == "-":
        return _parse_sequence(lines, pos, child_indent), end
    if _is_key_line(body):
        return _parse_mapping(lines, pos, child_indent), end
    return _fold([_dedent(line, child_indent) for line in lines[pos:end]]).strip(), end


def _parse_sequence(lines: List[str], pos: int, indent: int) -> List[Any]:
    items: List[Any] = []
    while pos < len(lines):
        line = lines[pos]
        if not line.strip():
            pos += 1
            continue
        if _indent_of(line) < indent:
            break
        stripped = line.strip()
        if not stripped.startswith("-"):
            break
        current = _indent_of(line)
        value = stripped[1:].strip()
        pos += 1
        if not value:
            value, pos = _read_nested(lines, pos, current)
        elif _BLOCK_SCALAR_RE.fullmatch(value):
            value, pos = _read_block_scalar(lines, pos, current, value)
        elif _is_key_line(value):
            # "- key: value" — a mapping that starts on the dash line.
            key, _, rest = value.partition(":")
            value = {key.strip(): _scalar(rest)}
        items.append(value)
    return items


def _parse_mapping(lines: List[str], pos: int, indent: int) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    while pos < len(lines):
        line = lines[pos]
        if not line.strip():
            pos += 1
            continue
        current = _indent_of(line)
        if current < indent:
            break
        if current > indent:
            # Over-indented with no owning key: skip it rather than let it
            # overwrite the previous entry.
            pos += 1
            continue
        match = _KEY_RE.match(line.strip())
        if not match:
            pos += 1
            continue
        key = _unquote(match.group("key").strip())
        raw_value = (match.group("value") or "").strip()
        pos += 1
        if _BLOCK_SCALAR_RE.fullmatch(raw_value):
            data[key], pos = _read_block_scalar(lines, pos, current, raw_value)
            continue
        if raw_value:
            value: Any = _scalar(raw_value)
            # A key with an inline value may still wrap onto deeper lines.
            end, _ = _block_extent(lines, pos, current)
            if end > pos:
                deeper_lines = [item for item in lines[pos:end] if item.strip()]
                # Only fold continuation prose; a nested key starts a new entry.
                if deeper_lines and not _is_key_line(deeper_lines[0]):
                    deeper = _indent_of(deeper_lines[0])
                    value = (
                        value + " " + _fold([_dedent(i, deeper) for i in deeper_lines])
                    ).strip()
                    pos = end
        else:
            value, pos = _read_nested(lines, pos, current)
        data[key] = value
    return data


def _parse_frontmatter(text: str) -> dict:
    """Parse the frontmatter block into a (possibly nested) dict.

    Handles the shapes skill authors actually write: block scalars
    (``description: >-``), plain scalars wrapped onto indented lines, nested
    maps (``metadata:`` with children), sequences, and quoted strings. A
    line-at-a-time ``partition(":")`` cannot: it captures the block-scalar
    indicator as the value, or drops the indented lines entirely.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    return _parse_mapping(match.group(1).splitlines(), 0, 0)


def _body_of(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1).strip()


def _as_text(value: Any) -> str:
    """Frontmatter values arrive as scalars; a stray mapping becomes its JSON."""
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (dict, list)):
        return "" if not value else str(value)
    return str(value)


def _as_list(value: Any) -> List[str]:
    """A name list from frontmatter: YAML sequence, "[a, b]", or "a, b"."""
    if isinstance(value, list):
        items = [str(item) for item in value]
    elif value is None or isinstance(value, dict):
        return []
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = text.split(",")
    out: List[str] = []
    for item in items:
        name = item.strip().strip("\"'")
        if name and name not in out:
            out.append(name)
    return out


def load_skill_file(path: Path) -> Optional[Skill]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta = _parse_frontmatter(text)
    body = _body_of(text)
    name = meta.get("name") or path.parent.name
    description = _as_text(meta.get("description")).strip()
    # The trigger decision rests on the description plus the body's opening.
    trigger_excerpt = (description + "\n" + body[:TRIGGER_BODY_CHARS]).strip()
    no_self = _as_text(meta.get("disable-model-invocation")).strip().lower() == "true"
    return Skill(
        name=name,
        description=description,
        trigger_excerpt=trigger_excerpt,
        path=path,
        disable_model_invocation=no_self,
        description_parse_failed=len(description) < MIN_MEANINGFUL_DESCRIPTION,
        requires=_as_list(meta.get("requires")),
    )


def load_skill_dir(directory: Path, recursive: bool = False) -> List[Skill]:
    """Every SKILL.md under a directory, in file-name order.

    ``recursive`` walks nested layouts (plugin caches keep skills at
    ``<vendor>/<plugin>/<version>/SKILL.md``) up to ``MAX_SKILL_DEPTH``.
    When two paths yield the same skill name — a versioned cache holding
    several builds of one skill — the first in sorted order wins and the
    rest are dropped, so one logical skill stays one catalog entry.
    """
    if not directory.is_dir():
        return []
    if recursive:
        found = directory.glob("**/SKILL.md")
        files = sorted(
            (p for p in found
             if len(p.relative_to(directory).parts) <= MAX_SKILL_DEPTH + 1),
            key=lambda p: p.parts)
    else:
        files = sorted(directory.glob("*/SKILL.md"))
    out: List[Skill] = []
    seen: set = set()
    for skill_md in files:
        skill = load_skill_file(skill_md)
        if skill and skill.name not in seen:
            seen.add(skill.name)
            out.append(skill)
    return out


def load_catalog(extra_dirs: Optional[List[Path]] = None) -> List[Skill]:
    defaults = [Path(p).expanduser() for p in DEFAULT_SKILL_DIRS]
    supplied = [Path(p).expanduser() for p in (extra_dirs or [])]
    skills: Dict[str, Skill] = {}
    # The built-in dirs are known to be flat; explicitly supplied ones get
    # the deep walk so a nested plugin cache still resolves.
    for directory, deep in [(d, False) for d in defaults] + [(d, True) for d in supplied]:
        for skill in load_skill_dir(directory, recursive=deep):
            if skill.name not in skills:
                skills[skill.name] = skill
    return list(skills.values())


def _injected_skill(name: str, entry: dict) -> Optional[Skill]:
    if not isinstance(entry, dict):
        return None
    description = str(entry.get("description") or "").strip()
    intent = entry.get("intent")
    intent = str(intent).strip() if isinstance(intent, str) and intent.strip() else None
    body_path = entry.get("body_path")
    path = Path(str(body_path)).expanduser() if body_path else Path("<injected>")
    body = ""
    if body_path:
        try:
            body = _body_of(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            # A declared but unreadable body costs the entry its activation
            # estimate; the description alone still carries the routing text.
            body = ""
    if not description and not body:
        return None
    trigger = (description + "\n" + body[:TRIGGER_BODY_CHARS]).strip()
    return Skill(
        name=name,
        description=description,
        trigger_excerpt=trigger,
        path=path,
        source="injected",
        intent=intent,
        description_parse_failed=len(description) < MIN_MEANINGFUL_DESCRIPTION,
    )


def parse_injected(raw) -> List[Skill]:
    """Build skills from already-decoded injected JSON.

    Shape::

        {"skill-name": {"description": "...", "intent": "...",
                        "body_path": "..."}, ...}

    An optional ``{"skills": {...}}`` wrapper is accepted too. Malformed
    entries are skipped rather than raising: one bad entry must not take the
    whole audit down, and its absence stays visible as a missing skill.
    """
    if not isinstance(raw, dict):
        return []
    entries = raw.get("skills") if isinstance(raw.get("skills"), dict) else raw
    out = []
    for name, entry in entries.items():
        if not isinstance(name, str):
            continue
        skill = _injected_skill(name, entry)
        if skill:
            out.append(skill)
    return out


def load_injected(path: Path) -> List[Skill]:
    """Read externally supplied skill text from a JSON file."""
    try:
        raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return parse_injected(raw)


def merge_catalog(
    catalog: List[Skill], injected: Optional[List[Skill]] = None
) -> List[Skill]:
    """Combine scanned and injected skills.

    An injected entry replaces a catalog entry of the same name — the point
    of supplying the text is to correct the parser, not to sit beside it.
    Names absent from the catalog are appended, which is how skills the
    scanned directories never see become auditable at all.
    """
    if not injected:
        return list(catalog)
    merged: Dict[str, Skill] = {}
    for skill in catalog:
        merged[skill.name.lower()] = skill
    for skill in injected:
        merged[skill.name.lower()] = skill
    return list(merged.values())


def parse_failures(skills: List[Skill]) -> List[Skill]:
    """Skills whose routing wording did not survive parsing."""
    return [s for s in skills if s.description_parse_failed]
