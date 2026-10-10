"""Which operator utterances are interventions, and of what form.

The harness writes to the same channel the operator types into, so a
``bucket == "user"`` item is not by itself evidence that a human said
anything: over the discovered corpus the largest single class on that channel
is a goal nudge the harness re-emits every turn. This module separates the two
with rules alone. No model is called, and every label carries the rule that
fired plus a verbatim quote, so a wrong label can be traced to a wrong rule
instead of being argued about.

Rules are ordered and the first match wins, which makes the order part of the
behaviour: a machine marker is checked before any judgement about how the
sentence reads, because the marker is a fact about who wrote it and the
reading is only an inference.

The same distinction applies inside one record. The harness wraps what the
operator typed in a slash-command envelope, a text-selection block, an
attachment listing or an image placeholder, and the wrapper is machine-written
even when the words inside it are not. Those wrappers are stripped before any
rule reads the text, so a rule never has to know about them -- and the stripped
result is carried alongside the label, because a reading taken from what is
left over is weaker evidence than one taken from raw keystrokes.
"""

import re
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Tuple


@dataclass(frozen=True)
class Classification:
    """One label plus what justifies it.

    ``evidence`` is a verbatim substring of the operator's own words -- of
    ``strip_carriers(text).lead``, or of the record itself when stripping left
    no words behind, as with a bare slash command. It is never a paraphrase, so
    it can be quoted straight back at the transcript, and it is empty when
    nothing matched.

    ``carriers`` names the wrappers that were removed to reach those words, in
    the order they were looked for. It is empty when the text was read as-is.
    """

    label: str
    rule: str
    evidence: str
    carriers: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Stripped:
    """What the operator actually typed, separated from what wrapped it.

    ``lead`` is byte-identical to the input when no carrier was found, which is
    what lets every rule ignore carriers entirely. ``command`` is the slash
    command as written, slash included, or ``None`` when the record is not a
    command envelope.
    """

    lead: str
    carriers: Tuple[str, ...]
    command: Optional[str]


# Both orders occur in the corpus, and the args element is absent entirely in
# one of them, so the two halves are matched independently rather than as one
# envelope pattern.
_COMMAND_NAME = re.compile(r"<command-name>\s*(/[^<]*?)\s*</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>(.*?)(?:</command-args>|\Z)", re.S)

# The attachment pattern stops at the end of the listing instead of running to
# end of text, so words the operator typed after it are not swallowed.
_CARRIERS = (
    ("selection", re.compile(r"<选中文本[^>]*>.*?(?:</选中文本>|\Z)", re.S)),
    ("image_block", re.compile(r"<image\b[^>]*>.*?(?:</image>|\Z)", re.S)),
    ("image_ref", re.compile(r"\[Image #\d+\]")),
    ("attachment", re.compile(r"附件引用：\s*(?:-\s*文件：[^\n]*\n?)+")),
)


def strip_carriers(text: str) -> Stripped:
    """Remove the machine-written wrappers and report which ones were there."""
    lead = text
    carriers: List[str] = []
    command: Optional[str] = None

    matched = _COMMAND_NAME.search(text)
    if matched is not None:
        command = matched.group(1)
        carriers.append("command")
        args = _COMMAND_ARGS.search(text)
        lead = args.group(1) if args is not None else ""

    for name, pattern in _CARRIERS:
        lead, found = pattern.subn(" ", lead)
        if found:
            carriers.append(name)

    if carriers:
        lead = re.sub(r"\s+", " ", lead).strip()
    return Stripped(lead=lead, carriers=tuple(carriers), command=command)


def _marker_rule(name: str, label: str,
                 *markers: str) -> Tuple[str, str, Callable[[str], Optional[str]]]:
    """A rule that fires on whichever of ``markers`` the text contains first."""

    def match(text: str) -> Optional[str]:
        for marker in markers:
            if marker in text:
                return marker
        return None

    return (name, label, match)


_EDGE_PUNCTUATION = "。．.！!？?，,、；;：:~ \t"

_STATUS_KEYWORDS = ("进展", "进度", "如何了", "咋样了", "怎么样了", "还需要做什么")
# Every real prod in the corpus is 10 chars or shorter; the only texts carrying a
# status keyword without being a prod are 200+ char briefs about "最新进展".
_STATUS_MAX_CHARS = 40

# Anchored on purpose: "checkout 3fafc873" and "commit ， push 两个remote" name
# something the agent did not have, so a parameter turns an errand into content.
_ERRANDS = frozenset((
    "push", "ci push", "commit push", "commit and push", "push github",
    "ls", "exit", "services", "提交一下", "提交到 main", "同步",
))

_GO_AHEAD_TOKENS = frozenset(("ok", "继续", "开始", "开工", "要"))

# A closed enumeration, not a model of acknowledgement: anything outside it stays
# unclassified and is counted as residue. Widening it is what the residue rate
# in the coverage report is for, and a wider rule needs its own failing test.
_GO_AHEAD_PHRASES = frozenset((
    "ok， 实现吧", "开始落地", "按照你的建议继续", "我看过了， 可以的，你继续吧",
    "同意写入和执行操作", "允许使用工具执行生成与验证", "批准方案 a，开始写 spec",
    "进入实施计划", "继续完成所有任务", "先等代理评审完成",
    "start subagent-driven execution", "enable tools to resume implementation",
))

_MENU_PICK = re.compile(r"^[a-z0-9]$")

# zsh prints "user@host cwd %" ahead of each pasted command. Anchored because a
# scan found the shape at position 0 in all seven paste-backs in the corpus and
# nowhere else, so anchoring costs no recall; [ \t] rather than \s keeps the
# match on one line. % only, not bash's $: no fixture for it, and $ is common
# inside the pasted SQL.
_TERMINAL_PROMPT = re.compile(r"^\S+@\S+[ \t]+\S+[ \t]+%")

# Scheme-anchored on purpose: without one, "foo/bar?x=1" is not reliably
# separable from the operator's own question mark.
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")

# The clause the operator closed on: everything back to the last sentence
# terminator, or the last newline. Dots count, so a filename ends a clause.
_QUESTION_CLAUSE = re.compile(r"[^。．.！!？?\n]+[？?]$")

# Above this a lead is a multi-paragraph brief or a report pasted back from
# another agent, and the markers below belong to a clause inside it rather than
# to the operator's own act. The longest single-act lead carrying one is 238
# chars and the shortest paste-back carrying one is 702, so the cap sits in the
# gap between them. What it excludes stays residue rather than being guessed at:
# one record, one act is the most a marker can read.
_FORM_MAX_CHARS = 300

# Most specific first, so the evidence is the strongest marker available.
_CORRECTION_MARKERS = (
    re.compile("我说的是"),
    re.compile("我的意思"),
    re.compile("我指的是"),
    re.compile("为啥没"),
    # The lookbehind is what keeps 是不是 out: all 11 distinct 是不是 leads in
    # the corpus are questions, not pushback.
    re.compile(r"(?<!是)不是"),
)

# 别 is absent on evidence, not by oversight: it fires on six distinct leads and
# every one is the second syllable of 分别, 差别 or 识别, with false positives on
# both sides, so no lookahead or lookbehind rescues it.
_REJECTION_MARKERS = (
    re.compile("不要"),
    re.compile("不用"),
    re.compile("不做"),
)


def _lookup_form(text: str) -> str:
    """Lowercase, collapse whitespace and drop edge punctuation.

    Only the ends are stripped, so ``text.strip()`` remains a verbatim substring
    and can stand as evidence for whatever the lookup matched.
    """
    return re.sub(r"\s+", " ", text).strip().strip(_EDGE_PUNCTUATION).lower()


def _status_prod(text: str) -> Optional[str]:
    stripped = text.strip()
    # A lone question mark is the operator prompting a silent agent. It is
    # settled here so the menu-pick branch cannot read it as an option choice.
    if stripped and not stripped.strip("？?"):
        return stripped
    if len(stripped) > _STATUS_MAX_CHARS:
        return None
    for keyword in _STATUS_KEYWORDS:
        if keyword in text:
            return keyword
    return None


def _operational_errand(text: str) -> Optional[str]:
    if _lookup_form(text) in _ERRANDS:
        return text.strip()
    return None


def _acknowledgement(text: str) -> Optional[str]:
    form = _lookup_form(text)
    if not form:
        return None
    if (_MENU_PICK.match(form) or form in _GO_AHEAD_TOKENS
            or form in _GO_AHEAD_PHRASES):
        return text.strip()
    return None


def _form_marker(patterns, text: str) -> Optional[str]:
    if len(text.strip()) > _FORM_MAX_CHARS:
        return None
    for pattern in patterns:
        matched = pattern.search(text)
        if matched is not None:
            return matched.group(0)
    return None


def _correction(text: str) -> Optional[str]:
    return _form_marker(_CORRECTION_MARKERS, text)


def _rejection(text: str) -> Optional[str]:
    return _form_marker(_REJECTION_MARKERS, text)


def _terminal_paste_back(text: str) -> Optional[str]:
    # Not length-capped, unlike the form markers: the signature describes the
    # whole record rather than a clause inside it, so a longer paste is more
    # evidence, not less.
    matched = _TERMINAL_PROMPT.match(text)
    return matched.group(0) if matched is not None else None


def _terminal_question(text: str) -> Optional[str]:
    # The mark is read only at the end; the URL blank is a guard, not a recall
    # device. Also not length-capped, for the same reason as the paste-back.
    if not _URL.sub(" ", text).rstrip().endswith(("？", "?")):
        return None
    matched = _QUESTION_CLAUSE.search(text.rstrip())
    return matched.group(0).strip() if matched is not None else None


_RULES: List[Tuple[str, str, Callable[[str], Optional[str]]]] = [
    _marker_rule("goal_nudge", "noise",
                 "Continue working toward the active thread goal.",
                 "A goal has been set."),
    _marker_rule("environment_context", "noise", "<environment_context>"),
    _marker_rule("local_command_caveat", "noise",
                 "Caveat: The messages below were generated by the user"),
    _marker_rule("background_task_notification", "noise",
                 "[SYSTEM NOTIFICATION - NOT USER INPUT]"),
    _marker_rule("subagent_notification", "noise", "<subagent_notification>"),
    _marker_rule("resume_nudge", "noise", "Output token limit hit."),
    _marker_rule("structured_output_nag", "noise", "你尚未调用 mcp__"),
    _marker_rule("pause_notifier_nudge", "noise", "[内部指令·严格模式]"),
    # The leading newline is load-bearing: "Prompt version:" mid-sentence is the
    # operator talking about a prompt, not a prompt being logged.
    _marker_rule("staged_prompt_envelope", "noise",
                 "\nPrompt version: ", 'Return exactly JSON {"claims"'),
    # A prefix: the corpus carries two wordings of the interrupt marker.
    _marker_rule("interrupt_marker", "interrupt",
                 "[Request interrupted by user"),
    _marker_rule("operator_command", "self_serve",
                 "<bash-input>", "<local-command-stdout>",
                 "<bash-stdout>", "<bash-stderr>"),
    # Ahead of the shape-reading rules: pasted output is arbitrary text, so a
    # long paste carries 不是 or 不要 by accident and must not be read as a form.
    ("terminal_paste_back", "fact", _terminal_paste_back),
    ("status_prod", "status", _status_prod),
    ("operational_errand", "chore", _operational_errand),
    ("acknowledgement", "confirm", _acknowledgement),
    ("correction", "correction", _correction),
    ("rejection", "rejection", _rejection),
    # Last: every rule above has a sharper claim on a record that also ends
    # with a question mark -- a lone ？ is a prod, a correction or rejection
    # ending in one already carries its marker, and a paste-back is read from
    # its prompt.
    ("terminal_question", "question", _terminal_question),
]


def _cascade(text: str) -> Optional[Classification]:
    for name, label, match in _RULES:
        evidence = match(text)
        if evidence is not None:
            return Classification(label=label, rule=name, evidence=evidence)
    return None


def classify(text: str) -> Classification:
    stripped = strip_carriers(text)
    targets = [stripped.lead]
    # Nothing of the operator's own survived the strip, so the record is
    # entirely harness-written and the markers are read off it directly.
    if not stripped.lead.strip():
        targets.append(text)
    for target in targets:
        matched = _cascade(target)
        if matched is not None:
            return replace(matched, carriers=stripped.carriers)
    if stripped.command is not None and not stripped.lead.strip():
        return Classification(label="chore", rule="bare_slash_command",
                              evidence=stripped.command,
                              carriers=stripped.carriers)
    return Classification(label="unclassified", rule="unclassified",
                          evidence="", carriers=stripped.carriers)
