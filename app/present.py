"""What the user sees (operator 2026-11-17: one brand, chat UI). The answer is markdown; for a risk
history question the app adds the steps table straight from the engine's data. Sources, grounding,
trace and limits stay in the server log and the ?debug=1 view only.

Silent safety: the agent rewrites an answer once when a statement about this system isn't backed;
whatever is still unbacked here is held back (the sentence or table row is left out, with a short
note), and an answer with nothing left becomes the fallback.
"""

from __future__ import annotations

import re
from typing import Any

from . import wording


def display_cause(cause: str) -> str:
    """Operator 2026-11-18: the user never reads about the assistant itself. Display only (the steps
    stay verbatim in the contract, log and ?debug=1): "engine change impact-4: …" -> "scoring method
    change: …", ", learned in L5" dropped, "learned window" -> "observed window". Scores, objects and
    relationships are unchanged."""
    c = re.sub(r"^engine change\s+[\w.-]+:\s*", "scoring method change: ", cause)
    c = re.sub(r",\s*learned in L\d+\b", "", c)
    c = re.sub(r"\blearned (window|period)\b", r"observed \1", c)
    return c


def history_table(h: dict[str, Any] | None) -> str:
    """Operator 2026-11-16: the distinct score steps, from the engine's history (values verbatim)."""
    if not h or not h.get("steps"):
        return ""
    rows = ["| Date | Score | What changed |", "|---|---|---|"]
    for st in h["steps"]:
        older = " (older method)" if st.get("older_engine") else ""
        causes = "; ".join(display_cause(c) for c in st.get("causes") or []) or "—"
        rows.append(f"| {st.get('at_display') or st.get('at') or ''} | {st.get('from')} → {st.get('to')}{older} | "
                    f"{causes.replace('|', '/')} |")
    out = "\n".join(rows)
    if h.get("nothing_changed_since_last"):
        out += "\n\nNothing changed since the last assessment."
    return out


# Operator 2026-11-18: after a hold-back the answer still reads cleanly. A sentence that points back at
# what was just left out ("Those are the places …") goes too, numbered steps are renumbered, and a
# lead-in line ending in ":" with nothing left under it is removed.
BACK_REFERENCE_RE = re.compile(r"^(?:Those|These|That|This|They|Them|It|Such|Both|Either|Neither)\b")
STEP_RE = re.compile(r"^(\s*)(\d+)([.)])(\s+)(.*)$")


def _tidy(lines: list[str]) -> list[str]:
    out: list[str] = []
    n = 0
    for line in lines:
        m = STEP_RE.match(line)
        if m:
            n += 1
            out.append(f"{m.group(1)}{n}{m.group(3)}{m.group(4)}{m.group(5)}")
            continue
        if line.strip():
            n = 0
        out.append(line)
    # a lead-in ("To check:") whose list is gone
    tidy: list[str] = []
    for i, line in enumerate(out):
        if line.rstrip().endswith(":") and not line.lstrip().startswith("|"):
            nxt = next((x for x in out[i + 1:] if x.strip()), "")
            if not (STEP_RE.match(nxt) or re.match(r"^\s*[-*+]\s", nxt) or nxt.lstrip().startswith("|")):
                continue
        tidy.append(line)
    # no runs of blank lines
    return [x for i, x in enumerate(tidy) if x.strip() or (i and tidy[i - 1].strip())]


def _drop_tokens(md: str, tokens: set[str]) -> tuple[str, int]:
    """Leave out every sentence (or table row) that contains an unbacked token, then tidy up."""
    dropped = 0
    out_lines = []
    last_dropped = False
    for line in md.splitlines():
        if line.lstrip().startswith("|"):
            if any(t in line for t in tokens):
                dropped += 1
                continue
            out_lines.append(line)
            continue
        m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+|\s*#{1,6}\s+)?(.*)$", line)
        prefix, body = (m.group(1) or ""), m.group(2)
        if not body.strip():
            out_lines.append(line)
            continue
        kept = []
        for p in re.split(r"(?<=[.!?])\s+", body):
            if any(t in p for t in tokens) or (last_dropped and BACK_REFERENCE_RE.match(p)):
                dropped += 1
                last_dropped = True
                continue
            kept.append(p)
            last_dropped = False
        if kept:
            out_lines.append(prefix + " ".join(kept))
    return "\n".join(_tidy(out_lines)).strip(), dropped


def finalize(out: dict[str, Any]) -> str:
    """The markdown the user sees. Adds out["presentation"] (logged; ?debug=1)."""
    answer = out.get("answer", "")
    flags = [f for f in out["grounding"]["flags"] if f["kind"] != "evidence"]  # evidence ids are internal
    probs = wording.problems(answer)
    tokens = {f["token"] for f in flags if f["where"] == "answer" or f["where"].startswith("system fact")}
    tokens |= {p.split(": ", 1)[1] for p in probs}
    outcome = "rewritten" if out.get("rewritten") else "passed"
    if tokens:
        answer, dropped = _drop_tokens(answer, tokens)
        outcome = "held back"
        has_text = bool(re.sub(r"[\s|#*_\-]", "", answer))
        answer = (answer + "\n\n" + wording.HELD_BACK_NOTE) if has_text and dropped else (answer or "")
        if not has_text or wording.problems(answer):
            answer, outcome = wording.FALLBACK_ANSWER, "fallback"
    table = history_table(out.get("history"))
    if table and outcome != "fallback":
        answer = answer.rstrip() + "\n\n" + table
    out["presentation"] = {"outcome": outcome, "unbacked": sorted(tokens),
                           "first_draft_flags": out.get("first_draft_flags", [])}
    return answer


def debug(out: dict[str, Any]) -> dict[str, Any]:
    """For us only (server log, ?debug=1): sources, grounding outcome, trace, the engine's limits."""
    return {
        "system_facts": out.get("system_facts", []),
        "grounding": out.get("grounding"),
        "presentation": out.get("presentation"),
        "tool_trace": out.get("tool_trace", []),
        "coverage": out.get("coverage", []),
        "freshness": out.get("freshness", {}),
        "limits": out.get("limits", []),
        "usage": out.get("usage"),
        "timing_ms": out.get("timing_ms"),
        "model": out.get("model"),
    }
