"""Load the skill catalog from local skill directories.

Each skill is a directory containing SKILL.md: optional YAML frontmatter
(name, description) followed by markdown instructions. The description plus
the opening of the body is what an agent sees when deciding whether to load
the skill, so that is what we audit against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_SKILL_DIRS = ("~/.agents/skills", "~/.kimi/skills")

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    trigger_excerpt: str  # what the agent sees when deciding to trigger
    path: Path
    # frontmatter disable-model-invocation: true means the model may not load
    # this skill on its own — only the router (or the user) may name it.
    disable_model_invocation: bool = False


def _parse_frontmatter(text: str) -> dict:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    data = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_skill_file(path: Path) -> Optional[Skill]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta = _parse_frontmatter(text)
    body = FRONTMATTER_RE.sub("", text, count=1).strip()
    name = meta.get("name") or path.parent.name
    description = meta.get("description", "")
    # The trigger decision rests on the description plus the body's opening.
    trigger_excerpt = (description + "\n" + body[:800]).strip()
    no_self = meta.get("disable-model-invocation", "").lower() == "true"
    return Skill(
        name=name,
        description=description,
        trigger_excerpt=trigger_excerpt,
        path=path,
        disable_model_invocation=no_self,
    )


def load_catalog(extra_dirs: Optional[List[Path]] = None) -> List[Skill]:
    dirs = [Path(p).expanduser() for p in DEFAULT_SKILL_DIRS]
    if extra_dirs:
        dirs.extend(Path(d).expanduser() for d in extra_dirs)
    skills = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for skill_md in sorted(directory.glob("*/SKILL.md")):
            skill = load_skill_file(skill_md)
            if skill and skill.name not in skills:
                skills[skill.name] = skill
    return list(skills.values())
