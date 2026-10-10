# src/agent_session_detective/ir/items.py
"""Prompt-content item extraction: messages, tool results, attachment classes.

Item ids are ``<agent_id>:<wire_seq>:<index>`` with index counting every
*item* in a record in event order. Buckets are assigned explicitly: unknown
attachment kinds land in ``unattributed`` (D2), never in a plausible-looking
bucket. The signature channel and the envelope channel are counted separately
so that a skill body embedded in a reminder shows up as a conflict instead of
silently re-bucketing the reminder.

D1: a ``Read`` of a ``SKILL.md`` is remembered so the *result* item carries
the ``skill_id``; the call item does not, or every read execution would
inflate the skill's execution count.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..timeline import SKILL_CONTENT_RE, SKILL_FILE_RE
from .estimator import estimate
from .intervention import classify, strip_carriers
from .records import WireRecord
from .schema import UNATTRIBUTED, ContentItem, Intervention

BASE_DIRECTORY_RE = re.compile(r"Base directory for this skill:\s*(\S+)")
LISTING_LINE_RE = re.compile(r"^- (.+?): ")
STUB_RE = re.compile(r"^Launching skill: (.+)$")
NORM_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z?\s*\n")

SKIP_TEXT_KEYS = ("type", "filePath", "path", "name")
SKIP_ENTRY_KEYS = ("type", "path", "name")

# attachment type -> (bucket, kind, text_key, name_key, entries_key)
ATTACHMENT_CLASSES = {
    "skill_listing": ("skill", "skill_catalog", "content", None, None),
    "invoked_skills": ("skill", "skill_body", "content", "name", "skills"),
    "critical_system_reminder": ("inject", "reminder", "content", None, None),
    "task_reminder": ("inject", "reminder", "content", None, None),
    "goal_state": ("inject", "goal_state", None, None, None),
    "auto_mode": ("inject", "auto_mode", "reminderType", "reminderType", None),
    "auto_mode_exit": ("inject", "auto_mode_exit", None, None, None),
    "queued_command": ("inject", "queued_command", "prompt", None, None),
    "edited_text_file": ("inject", "file_snapshot", None, "filename", None),
    "file": ("inject", "file_snapshot", None, "filename", None),
    "post_compact_restored_files": ("inject", "compact_restore", "content", "filePath", "files"),
    "directory": ("inject", "directory_listing", None, "path", None),
    "date_change": ("inject", "date_change", None, None, None),
    "relevant_memories": ("inject", "memory", "content", "filePath", "files"),
    "hook_non_blocking_error": ("inject", "hook_error", None, "hookName", None),
    "mcp_instructions_delta": ("tools", "tool_schema", None, None, None),
    "agent_listing_delta": ("inject", "listing_delta", None, None, None),
}


def flatten_text(value, skip=()) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            part for part in (flatten_text(item, skip) for item in value) if part
        )
    if isinstance(value, dict):
        return "\n".join(
            part
            for key, item in value.items()
            if key not in skip
            for part in [flatten_text(item, skip)]
            if part
        )
    return ""


def attachment_text(attachment: dict, text_key: Optional[str]) -> str:
    if text_key is not None and isinstance(attachment.get(text_key), str):
        return attachment[text_key]
    return flatten_text(attachment, SKIP_TEXT_KEYS)


def skill_signature(text: str) -> Optional[Tuple[str, str]]:
    match = SKILL_CONTENT_RE.search(text)
    if match:
        name = match.group(1)
        return name.lower(), name
    match = BASE_DIRECTORY_RE.search(text)
    if match:
        name = os.path.basename(match.group(1).rstrip("/"))
        if name:
            return name.lower(), name
    return None


def normalize_text(text: str) -> str:
    text = NORM_TS_RE.sub("", text, count=1)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Extraction:
    items: List[ContentItem] = field(default_factory=list)
    listing_lines: Dict[str, List[Tuple[str, str, int]]] = field(default_factory=dict)
    envelope: int = 0
    signature: int = 0
    conflicts: int = 0
    human_text_dropped: int = 0
    interventions: List[Intervention] = field(default_factory=list)


def extract_items(records: List[WireRecord], agent_id: str) -> Extraction:
    extraction = Extraction()
    pending_case_reads: Dict[str, str] = {}
    part_index = 0

    def add(record, bucket, kind, text, channel, skill_id=None, name=None,
            by_signature=False, tool_use_id=None, human_text=None):
        nonlocal part_index
        if not isinstance(text, str):
            text = flatten_text(text)
        item = ContentItem(
            item_id="%s:%d:%d" % (agent_id, record.seq, part_index),
            agent_id=agent_id,
            bucket=bucket,
            kind=kind,
            name=name,
            channel=channel,
            wire_seq=record.seq,
            gone_seq=None,
            size_chars=len(text),
            tokens_est=estimate(text),
            sha1=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            norm_sha1=hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest(),
            record={"file": record.source, "seq": record.seq, "uuid": record.uuid},
            preview=text[:200],
            skill_id=skill_id,
            tool_use_id=tool_use_id,
            human_text=human_text,
        )
        extraction.items.append(item)
        part_index += 1
        if by_signature:
            extraction.signature += 1
        else:
            extraction.envelope += 1
        return item

    for record in records:
        part_index = 0
        for event in record.events:
            if event.type == "ContentPart":
                part_type = event.payload.get("type")
                if part_type == "text":
                    add(record, "assistant", "assistant_text",
                        event.payload.get("text") or "", "qoder:assistant")
                elif part_type == "think":
                    add(record, "assistant", "thinking",
                        event.payload.get("think") or "", "qoder:assistant")
            elif event.type == "ToolCall":
                function = event.payload.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or ""
                skill_id = None
                lowered = name.lower()
                if lowered == "skill":
                    try:
                        parsed = json.loads(arguments)
                    except (TypeError, ValueError):
                        parsed = {}
                    target = parsed.get("skill") if isinstance(parsed, dict) else None
                    if isinstance(target, str) and target:
                        skill_id = target.lower()
                elif lowered == "read":
                    try:
                        parsed = json.loads(arguments)
                    except (TypeError, ValueError):
                        parsed = {}
                    file_path = parsed.get("file_path") if isinstance(parsed, dict) else None
                    if isinstance(file_path, str) and SKILL_FILE_RE.search(file_path):
                        tool_call_id = event.payload.get("id")
                        if tool_call_id is not None:
                            pending_case_reads[tool_call_id] = file_path
                add(record, "assistant", "tool_call", arguments, "qoder:assistant",
                    skill_id=skill_id, name=name or None,
                    tool_use_id=event.payload.get("id"))
            elif event.type == "TurnBegin":
                texts = [
                    part.get("text")
                    for part in event.payload.get("user_input") or []
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                ]
                text = "\n".join(texts)
                ref = event.ref or {}
                signature = skill_signature(text)
                # TurnBegin items only, and only in the top-level conversation:
                # a user record can also yield tool_result items (double-counting
                # one utterance), and elsewhere this same channel carries the
                # parent agent's Task brief rather than operator keystrokes.
                human_text = ref.get("human_text")
                if human_text is not None and agent_id != "main":
                    human_text = None
                    extraction.human_text_dropped += 1
                if ref.get("is_compact_summary"):
                    add(record, "inject", "compact_summary", text,
                        "qoder:user:compact_summary", human_text=human_text)
                elif signature is not None:
                    add(record, "skill", "skill_body", text,
                        "qoder:signature:skill_body",
                        skill_id=signature[0], name=signature[1], by_signature=True,
                        human_text=human_text)
                else:
                    item = add(record, "user", "user_message", text, "qoder:user",
                               human_text=human_text)
                    # Classified here because the full text only exists here
                    # (preview truncates). Main-agent turns only: injected
                    # bodies and sidechain briefs are not operator keystrokes.
                    if agent_id == "main" and texts:
                        matched = classify(text)
                        lead_sha1 = hashlib.sha1(
                            strip_carriers(text).lead.encode("utf-8")
                        ).hexdigest()
                        extraction.interventions.append(
                            Intervention(
                                item_id=item.item_id,
                                label=matched.label,
                                rule=matched.rule,
                                evidence=matched.evidence,
                                carriers=list(matched.carriers),
                                lead_sha1=lead_sha1,
                            )
                        )
            elif event.type == "ToolResult":
                return_value = event.payload.get("return_value") or {}
                output = return_value.get("output")
                # "data" holds base64 image bytes, not text; media_type is metadata
                text = output if isinstance(output, str) else flatten_text(
                    output, ("type", "is_error", "data", "media_type"))
                tool_call_id = event.payload.get("tool_call_id")
                stub_match = STUB_RE.match(text.strip())
                if stub_match:
                    display = stub_match.group(1).strip()
                    add(record, "skill", "skill_stub", text, "qoder:tool_result",
                        skill_id=display.lower(), name=display,
                        tool_use_id=tool_call_id)
                elif tool_call_id is not None and tool_call_id in pending_case_reads:
                    file_path = pending_case_reads.pop(tool_call_id)
                    if return_value.get("is_error"):
                        add(record, "tool", "tool_result", text, "qoder:tool_result",
                            tool_use_id=tool_call_id)
                    else:
                        folder = os.path.basename(os.path.dirname(file_path.rstrip("/")))
                        add(record, "tool", "tool_result", text, "qoder:tool_result",
                            skill_id=folder.lower() or None, name=folder or None,
                            tool_use_id=tool_call_id)
                else:
                    add(record, "tool", "tool_result", text, "qoder:tool_result",
                        tool_use_id=tool_call_id)
            elif event.type == "Attachment":
                attachment = event.payload.get("attachment")
                if not isinstance(attachment, dict):
                    continue
                attachment_type = (
                    event.payload.get("attachment_type")
                    or attachment.get("type")
                    or "unknown"
                )
                channel = "qoder:attachment:%s" % attachment_type
                if attachment_type == "hook_output":
                    text = attachment_text(attachment, "output")
                    signature = skill_signature(text)
                    if signature is not None:
                        add(record, "skill", "skill_body", text, channel,
                            skill_id=signature[0], name=signature[1], by_signature=True)
                    else:
                        add(record, "inject", "hook_inline", text, channel)
                    continue
                entry = ATTACHMENT_CLASSES.get(attachment_type)
                if entry is None:
                    add(record, UNATTRIBUTED, "unknown",
                        attachment_text(attachment, None), channel)
                    continue
                bucket, kind, text_key, name_key, entries_key = entry
                if entries_key is not None:
                    entries = attachment.get(entries_key)
                    for entry_item in entries if isinstance(entries, list) else []:
                        if not isinstance(entry_item, dict):
                            continue
                        text = entry_item.get(text_key) if text_key else None
                        if not isinstance(text, str):
                            text = flatten_text(entry_item, SKIP_ENTRY_KEYS)
                        name = entry_item.get(name_key) if name_key else None
                        display = name if isinstance(name, str) and name else None
                        skill_id = None
                        if bucket == "skill":
                            signature = skill_signature(text)
                            if display is None and signature is not None:
                                display = signature[1]
                            if display is not None:
                                skill_id = display.lower()
                        add(record, bucket, kind, text, channel,
                            skill_id=skill_id, name=display)
                    continue
                text = attachment_text(attachment, text_key)
                name = attachment.get(name_key) if name_key else None
                signature = skill_signature(text)
                if signature is not None and bucket != "skill":
                    extraction.conflicts += 1
                item = add(record, bucket, kind, text, channel,
                           name=name if isinstance(name, str) and name else None)
                if kind == "skill_catalog":
                    lines = []
                    for line in text.splitlines():
                        stripped = line.strip()
                        match = LISTING_LINE_RE.match(stripped)
                        if match:
                            display = match.group(1).strip()
                            lines.append((display.lower(), display, estimate(stripped)))
                    if lines:
                        extraction.listing_lines[item.item_id] = lines
    return extraction
