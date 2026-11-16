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


def build(answer: dict[str, Any], trace: list[dict[str, Any]], results: list[dict[str, Any] | None]) -> dict[str, Any]:
    ok_results = [r for r in results if isinstance(r, dict)]

    coverage: list[dict[str, Any]] = []
    seen_cov: set[str] = set()
    freshness: dict[str, Any] = {}
    limits: list[str] = []
    valid_evidence: set[str] = set()
    for i, (t, r) in enumerate(zip(trace, results), start=1):
        valid_evidence.add(f"call:{i}")
        if not isinstance(r, dict):
            continue
        collect_evidence(r, valid_evidence)
        cov = r.get("coverage")
        if cov:
            key = json.dumps(cov, sort_keys=True)
            if key not in seen_cov:
                seen_cov.add(key)
                coverage.append({"from_call": i, "tool": t["tool"], **cov})
        for stage, f in (r.get("freshness") or {}).items():
            freshness.setdefault(stage, f)
        for lim in r.get("limits") or []:
            if lim not in limits:
                limits.append(lim)

    claims = answer.get("claims") or []
    texts = {"answer": answer.get("answer", "")}
    evidence_used = []
    for n, c in enumerate(claims, start=1):
        texts[f"claim {n}"] = c.get("text", "")
        evidence_used += [(f"claim {n}", e) for e in c.get("evidence_ids", [])]
    g = grounding.check(texts, ok_results, evidence_used, valid_evidence)

    return {
        "answer": answer.get("answer", ""),
        "claims": claims,
        "tool_trace": trace,
        "coverage": coverage,
        "freshness": freshness,
        "limits": limits,
        "grounding": g,
    }


# Operator 2026-11-16 (browser review): the answer text is plain language. Field names and JSON
# words stay in the evidence panel (claims, tool trace). Object names are upper case, so a lower-case
# snake_case word is a Graphene field name.
FIELD_NAME_RE = re.compile(r"(?<![A-Za-z0-9_:/.-])[a-z]+(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
JSON_WORD_RE = re.compile(r"\b(?:null|true|false|None|True|False)\b")


def developer_words(text: str) -> list[str]:
    """Field names and JSON words in an answer's text (should be none)."""
    return [m.group(0) for rx in (FIELD_NAME_RE, JSON_WORD_RE) for m in rx.finditer(text)]

