"""What a skin shows (operator 2026-11-17, white-label). The internal answer contract (claims,
tool_trace, grounding, coverage, freshness, limits, history) is built as before and logged; the
browser gets only the plain answer, its "How do I know?" sources in SAP terms, and when each area
was learned. Silent safety: if a name, number or date in what would be shown isn't in this
question's engine results, or the wording rules are broken, the answer is replaced by FALLBACK_ANSWER.
"""

from __future__ import annotations

import re
from typing import Any

from . import brand as brands
from .contract import developer_words

# Operator 2026-11-17: area names in the "What I've learned" sidebar (engine stage ids L0-L5).
# Other stages keep the engine's own name.
AREA_LABELS = {"L0": "System", "L1": "Dictionary", "L2": "Code", "L3": "Code relationships",
               "L4": "Transports", "L5": "Operations"}
_STAGE_RE = re.compile(r"\bL(\d{1,2})\b")
_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(min|h|d|w)\s*$")
_AGE_UNITS = {"min": "minute", "h": "hour", "d": "day", "w": "week"}


def area_label(stage: str, engine_name: str | None = None) -> str:
    return AREA_LABELS.get(stage) or engine_name or stage


def age_words(age: Any) -> str:
    """The engine's age text in words ("5 d" -> "5 days"). Wording only: the number is the engine's."""
    m = _AGE_RE.match(str(age or ""))
    if not m:
        return str(age or "")
    n, unit = m.group(1), _AGE_UNITS[m.group(2)]
    return f"{n} {unit}{'' if n == '1' else 's'}"


def _stage_words(text: str, names: dict[str, str]) -> str:
    """Stage ids (L5) in a displayed line become area names (Operations)."""
    return _STAGE_RE.sub(lambda m: area_label(f"L{m.group(1)}", names.get(f"L{m.group(1)}")), text)


def learned_for_answer(out: dict[str, Any]) -> list[dict[str, str]]:
    """Which learned areas this answer used, and how long ago each was learned (engine values)."""
    names: dict[str, str] = {}
    for cov in out.get("coverage") or []:
        for stage, info in (cov.get("this_answer_uses") or {}).items():
            names.setdefault(stage, (info or {}).get("name") or stage)
    rows = []
    for stage, name in names.items():
        age = ((out.get("freshness") or {}).get(stage) or {}).get("age")
        rows.append({"area": area_label(stage, name), "age": age_words(age) if age else ""})
    return rows


def view(out: dict[str, Any], brand: brands.Brand) -> dict[str, Any]:
    """The answer as this skin shows it, or the fallback. Adds out["presentation"] (logged only)."""
    reasons: list[str] = []
    # Names, numbers and dates are what the user sees; an evidence-id flag is internal only.
    shown_flags = [f for f in out["grounding"]["flags"] if f["kind"] != "evidence"]
    reasons += [f"not backed by the facts: {f['token']} ({f['where']})" for f in shown_flags]
    answer = out.get("answer", "")
    reasons += brands.wording_problems(answer, brand)
    reasons += [f"field name / JSON word: {w}" for w in developer_words(answer)]

    stage_names = {s: n for s, n in ((k, v) for cov in out.get("coverage") or []
                                     for k, v in ((st, (i or {}).get("name", st))
                                                  for st, i in (cov.get("this_answer_uses") or {}).items()))}
    sources, dropped = [], []
    for c in out.get("claims") or []:
        t = _stage_words(str(c.get("text", "")).strip(), stage_names)
        bad = brands.wording_problems(t, brand) + developer_words(t)
        (dropped if bad else sources).append(t)
    history = None
    if out.get("history") and out["history"].get("steps"):
        h = out["history"]
        steps = [{"date": st.get("at_display") or "", "date_full": st.get("at") or "", "from": st.get("from"),
                  "to": st.get("to"), "older_method": bool(st.get("older_engine")),
                  "causes": [_stage_words(x, stage_names) for x in st.get("causes") or []]} for st in h["steps"]]
        for st in steps:
            for x in st["causes"]:
                reasons += [f"history: {p}" for p in brands.wording_problems(x, brand)]
        history = {"transport": h.get("transport"), "steps": steps,
                   "nothing_changed_since_last": h.get("nothing_changed_since_last")}

    shown = not reasons
    out["presentation"] = {"brand": brand.key, "shown": shown, "reasons": reasons, "sources_dropped": dropped}
    if not shown:
        return {"answer": brands.FALLBACK_ANSWER, "fallback": True, "sources": [], "learned": [], "history": None}
    return {"answer": answer, "fallback": False, "sources": sources, "learned": learned_for_answer(out),
            "history": history}


def learned_overview(status: dict[str, Any] | None) -> dict[str, Any]:
    """The "What I've learned" sidebar and the status dot, from the engine's status result only."""
    res = (status or {}).get("result") or {}
    areas, latest = [], None
    for s in res.get("stages") or []:
        last = s.get("last_success") or {}
        done = s.get("status") == "done"
        areas.append({"area": area_label(s.get("stage", ""), s.get("name")), "learned": done,
                      "age": age_words((last.get("age") or {}).get("text")) if done else ""})
        if done and last.get("finished_at") and (latest is None or last["finished_at"] > latest[0]):
            latest = (last["finished_at"], age_words((last.get("age") or {}).get("text")))
    return {"sid": (res.get("tenant") or {}).get("sid"), "areas": areas,
            "last_learned": latest[1] if latest else None}
