"""Wording rules for every answer (operator 2026-11-18): the assistant never talks about itself or how
it knows ("learned", "trained", "AI model", "engine", "my data", tool names); no ids, field names or
JSON words. Used by the silent check before an answer is shown (rewrite once, then hold back)."""

from __future__ import annotations

import re

from .contract import developer_words

FORBIDDEN_WORDING_RE = re.compile(
    r"\b(?:trained|training|fine[- ]?tun(?:ed|ing)|learn(?:ed|ing|t)|AI model|language model|our own AI"
    r"|engine|knowledge base|Graphene|Claude|Anthropic|my (?:own )?(?:\w+ )?data|my tools?"
    r"|What I don['’]t know)\b", re.I)
INTERNAL_RE = re.compile(r"\b(?:find_object|assess_transport|system_profile|MCP|call:\d+|result \d+|tool_trace|evidence_ids?)\b")
# Operator 2026-11-18: dates in words only; "Not available", never a "Gaps" label.
NUMERIC_DATE_RE = re.compile(r"(?<![\w.-])(?:\d{4}-\d{2}-\d{2}|\d{4}\.\d{2}\.\d{2}|\d{1,2}\.\d{1,2}\.\d{4})(?![\w-])")
# Operator 2026-11-18: no "never read" wording anywhere; say what isn't available instead.
# Operator 2026-11-18: never describe the lookups themselves.
TOOL_TALK_RE = re.compile(r"\b(?:look ?ups? (?:work|only|are limited)|by name or key|(?:no |without a )?listing by (?:object )?type"
                          r"|can(?:no|['’])t list|cannot list|only (?:look up|search) by name)\b", re.I)
# Operator 2026-11-18 (principle 2 and 5): no refusal sentence, never what is "recorded" or "permitted".
REFUSAL_RE = re.compile(r"\bI (?:can['’]t|cannot|can not|am not able to|['’]m not able to|am unable to|['’]m unable to) "
                        r"(?:open|see|access|read|list|show|view|check|review|get)\b"
                        r"|\b(?:isn['’]t|aren['’]t|is not|are not|not|never) (?:recorded|permitted)\b", re.I)
NEVER_READ_RE = re.compile(r"\bnever (?:read|accessed)\b|\b(?:isn['’]t|aren['’]t|is not|are not) read\b", re.I)
GAPS_LABEL_RE = re.compile(r"(?mi)^[\W_]*(?:main\s+)?gaps?\b[^.\n]*:")
# A reply that only promises to look something up (browser testing 2026-11-17).
# "Paste its text here and I'll check it against your system" (SAP Note number, 2026-11-18) is not one.
PROMISE_RE = re.compile(r"\b(?:I['’]ll|I will|let me)\s+(?:check|look|find|verify|get back|search)\b"
                        r"(?!\s+it against\b)", re.I)

FALLBACK_ANSWER = "I'm not certain about that one. Can you rephrase or name a specific object?"
NOT_READY_ANSWER = "I'm still connecting to your SAP system. Please ask again in a moment."
ERROR_ANSWER = "I couldn't answer that just now. Please try again."
HELD_BACK_NOTE = "_I've left out a detail about your system that I couldn't confirm._"


def problems(text: str) -> list[str]:
    """Why this answer text may not be shown as is (empty = fine)."""
    out = [f"wording: {m.group(0)}" for m in FORBIDDEN_WORDING_RE.finditer(text)]
    out += [f"internal: {m.group(0)}" for m in INTERNAL_RE.finditer(text)]
    out += [f"numeric date: {m.group(0)}" for m in NUMERIC_DATE_RE.finditer(text)]
    out += [f"label: {m.group(0).strip()}" for m in GAPS_LABEL_RE.finditer(text)]
    out += [f"wording: {m.group(0)}" for m in NEVER_READ_RE.finditer(text)]
    out += [f"wording: {m.group(0)}" for m in TOOL_TALK_RE.finditer(text)]
    out += [f"wording: {m.group(0)}" for m in REFUSAL_RE.finditer(text)]
    out += [f"field name / JSON word: {w}" for w in developer_words(_strip_code(text))]
    return out


def _strip_code(md: str) -> str:
    """Profile parameters in code (e.g. `icm/HTTP/websocket_timeout`, with a slash) are SAP text, not
    field names; any other code span is still checked (a field name in backticks is still one)."""
    return re.sub(r"`[^`\n]*/[^`\n]*`", " ", md)


_ESCAPED_NL = re.compile(r"\\n")


def fix_escaped_newlines(md: str) -> str:
    """eval 2026-11-18: the model sometimes writes line breaks as the two characters backslash-n
    inside its JSON answer, which the browser would show as text. Outside code spans they become real
    line breaks."""
    if "\\n" not in md:
        return md
    parts = re.split(r"(`[^`]*`)", md)
    return "".join(p if p.startswith("`") else _ESCAPED_NL.sub("\n", p) for p in parts)
