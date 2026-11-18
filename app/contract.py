"""Builds the answer contract from Claude's structured answer plus Graphene's
tool results. coverage, freshness and limits are copied verbatim from Graphene;
this module never computes or rewrites a fact.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import grounding

SUMMARY_KEYS = ("found", "message", "total_affected", "showing", "score", "band", "percentile",
                "history_size", "stages_complete")


def short_result(structured: dict[str, Any] | None, error: str | None = None) -> str:
    """A one-line view of a tool result for the trace, using Graphene's own values."""
    if error:
        return f"error: {error}"[:240]
    if not isinstance(structured, dict):
        return ""
    res = structured.get("result") if isinstance(structured.get("result"), dict) else {}
    parts: list[str] = []
    scopes = [res, res.get("assessment") if isinstance(res.get("assessment"), dict) else {},
              structured.get("coverage") or {}]
    for scope in scopes:
        for k in SUMMARY_KEYS:
            if k in scope and not isinstance(scope[k], (dict, list)):
                parts.append(f"{k}: {scope[k]}")
    if "entry_points" in res:
        parts.append(f"entry_points: {', '.join(res['entry_points']) or 'none'}")
    if isinstance(res.get("dynamic_calls"), list):
        parts.append(f"dynamic_calls: {len(res['dynamic_calls'])} listed")
    ev = structured.get("evidence_ids") or []
    parts.append(f"{len(ev)} evidence ids")
    return " · ".join(dict.fromkeys(parts))[:400]


def collect_evidence(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("evidence_ids", "evidence") and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            else:
                collect_evidence(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_evidence(v, out)


# Graphene change kinds that are effects or reference lines, not causes of a score step.
_NOT_CAUSES = ("score", "band", "factor raw value")


def _cause(ch: dict[str, Any]) -> list[str]:
    """Graphene's own words for one change, with only connecting labels added by the app."""
    what = str(ch.get("what", ""))
    if what in _NOT_CAUSES or what.startswith("band line"):
        return []
    if ch.get("changelog"):
        return [f"engine change {line}" for line in ch["changelog"]]
    if what == "new affected object":
        out = f"new affected object {ch.get('key', '')}"
        if ch.get("via"):
            out += f" via {ch['via']}"
        if ch.get("learned_in"):
            out += f", learned in {ch['learned_in']}"
        return [out]
    if "from" in ch and "to" in ch:
        return [f"{what} {ch['from']} → {ch['to']}"]
    return [f"{what} {ch.get('key', '')}".strip()]


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UTC_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|\+00:00)$")


def display_utc(ts: Any) -> str:
    """Operator 2026-11-16: "2026-11-12T12:53:47+00:00" -> "12 Nov 2026, 12:53 UTC". Display formatting
    only: no time-zone conversion; a timestamp that isn't UTC is shown exactly as Graphene gave it."""
    m = _UTC_TS.match(str(ts or ""))
    if not m:
        return str(ts or "")
    y, mo, d, hh, mm = m.groups()
    return f"{int(d)} {_MONTHS[int(mo) - 1]} {y}, {hh}:{mm} UTC"


def history_steps(results: list[dict[str, Any]], transport: str) -> dict[str, Any] | None:
    """Operator 2026-11-16: the distinct score steps of a transport's history, rendered by the app
    from Graphene's data (date, score from → to, cause), never written by the model. Values verbatim."""
    want = (transport or "").strip().upper()
    if not want:
        return None
    for r in results:
        if r.get("tool") != "assess_transport":
            continue
        res = r.get("result") or {}
        if str(res.get("transport", "")).upper() != want or not isinstance(res.get("history"), list):
            continue
        steps = []
        for e in res["history"]:
            changes = e.get("changes_from_previous") or []
            score = next((c for c in changes if c.get("what") == "score"), None)
            if not score:
                continue
            band = next((c for c in changes if c.get("what") == "band"), None)
            causes = [x for c in changes for x in _cause(c)]
            steps.append({"at": e.get("assessed_at"), "at_display": display_utc(e.get("assessed_at")), "from": score.get("from"), "to": score.get("to"),
                          "band_from": band.get("from") if band else None, "band_to": band.get("to") if band else None,
                          "older_engine": e.get("comparable") is False, "causes": causes})
        csp = res.get("changes_since_previous")
        return {"transport": res.get("transport"), "steps": steps,
                "nothing_changed_since_last": csp == [] if isinstance(csp, list) else None}
    return None


def _id_mentions(ev_id: str, names: set[str]) -> int:
    return sum(1 for n in names if re.search(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])", ev_id))


def fill_evidence(claim: dict[str, Any], results: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Operator 2026-11-17: the model cites results by number ("results": [1]); the app fills in the
    real evidence ids from those results, never typed by the model. Per cited result: the ids that
    name the most objects of the claim (node ids preferred when only one object matches); if none
    names a claim object, "call:N". A number that is not a result of this question stays as
    "result N" and is flagged by the grounding check."""
    if "evidence_ids" in claim and "results" not in claim:  # already filled (re-built contract)
        return claim
    names: set[str] = set()
    for n in grounding._names_in(claim.get("text", "")):
        names.add(n)
        if ":" in n:
            names.add(n.split(":", 1)[1])
    # numbers matter for facts without object names (e.g. node:KERNEL:ZUZ:753:700)
    names |= {m.group(0) for m in grounding.NUM_RE.finditer(claim.get("text", "")) if len(m.group(0)) >= 2}
    ids: list[str] = []
    for num in claim.get("results") or []:
        if not isinstance(num, int) or not (1 <= num <= len(results)) or not isinstance(results[num - 1], dict):
            ids.append(f"result {num}")
            continue
        pool: set[str] = set()
        collect_evidence(results[num - 1], pool)
        scored = [(_id_mentions(e, names), e) for e in sorted(pool)] if names else []
        best = max((s for s, _ in scored), default=0)
        picked = [e for s, e in scored if s == best and best > 0]
        if best == 1 and any(e.startswith("node:") for e in picked):
            picked = [e for e in picked if e.startswith("node:")]
        ids += picked or [f"call:{num}"]
    return {"text": claim.get("text", ""), "results": claim.get("results") or [],
            "evidence_ids": list(dict.fromkeys(ids))}


def build(answer: dict[str, Any], trace: list[dict[str, Any]], results: list[dict[str, Any] | None],
          first_n: int = 1, user_texts: list[str] | None = None) -> dict[str, Any]:
    """The internal answer contract. results: every engine result of this chat, numbered from 1
    ("result N"); this message's own calls start at first_n. user_texts: the user's own messages in
    this chat (names and numbers from them may be repeated)."""
    ok_results = [r for r in results if isinstance(r, dict)]
    valid_evidence: set[str] = {f"call:{i}" for i in range(1, len(results) + 1)}
    for r in ok_results:
        collect_evidence(r, valid_evidence)

    coverage: list[dict[str, Any]] = []
    seen_cov: set[str] = set()
    freshness: dict[str, Any] = {}
    limits: list[str] = []
    for t, r in zip(trace, results[first_n - 1:]):
        if not isinstance(r, dict):
            continue
        cov = r.get("coverage")
        if cov:
            key = json.dumps(cov, sort_keys=True)
            if key not in seen_cov:
                seen_cov.add(key)
                coverage.append({"from_result": t["n"], "tool": t["tool"], **cov})
        for stage, f in (r.get("freshness") or {}).items():
            freshness.setdefault(stage, f)
        for lim in r.get("limits") or []:
            if lim not in limits:
                limits.append(lim)

    facts = [fill_evidence(c, results) for c in answer.get("system_facts") or []]
    evidence_used = [(f"system fact {n}", e) for n, c in enumerate(facts, start=1) for e in c["evidence_ids"]]
    g = grounding.check_system(answer.get("answer", ""), [c["text"] for c in facts], ok_results,
                               user_texts or [], evidence_used, valid_evidence)
    return {
        "answer": answer.get("answer", ""),
        "system_facts": facts,
        "tool_trace": trace,
        "coverage": coverage,
        "freshness": freshness,
        "limits": limits,
        "grounding": g,
        "history": history_steps(ok_results, answer.get("history_for", "")),
    }


# Operator 2026-11-16 (browser review): the answer text is plain language. Field names and JSON
# words stay in the evidence panel (claims, tool trace). Object names are upper case, so a lower-case
# snake_case word is a Graphene field name.
FIELD_NAME_RE = re.compile(r"(?<![A-Za-z0-9_:/.-])[a-z]+(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
# lower case only: "None." / "True" are English at a sentence start (eval 2026-11-18: "**Alerts** None.")
JSON_WORD_RE = re.compile(r"\b(?:null|true|false)\b")


def developer_words(text: str) -> list[str]:
    """Field names and JSON words in an answer's text (should be none)."""
    return [m.group(0) for rx in (FIELD_NAME_RE, JSON_WORD_RE) for m in rx.finditer(text)]

