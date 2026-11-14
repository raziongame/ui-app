"""Grounding check: every object/transport name and number in an answer must
appear in this question's Graphene tool results. Anything else is flagged
(shown yellow in the UI), never hidden or corrected.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Object-like names: upper-case identifiers that contain a digit or underscore
# (ZS4_IOT_DATA, ZUZK900008, L6), optionally with a Graphene type prefix
# (FUNC:ZS4_IOT_INSERT_DATA). Plus pseudonyms such as JOB-5192e9d4d2ab / U-1a2b.
NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:(?:[A-Z]{2,5}):)?"
    r"(?=[A-Z0-9_/]*[_0-9])[A-Z][A-Z0-9_/]{1,}"
    r"(?![A-Za-z0-9_])"
)
PSEUDO_RE = re.compile(r"(?<![A-Za-z0-9])(?:JOB|U)-[0-9a-f]{4,}(?![0-9a-f])")
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.:/-])-?\d+(?:\.\d+)?(?![0-9])")

# Words that look like names but are ordinary vocabulary in answers.
IGNORE_NAMES: set[str] = set()


def _walk(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, list):
        yield ("__len__", len(obj))
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _norm_num(s: str | float | int) -> str:
    f = float(s)
    return repr(f) if f != int(f) else str(int(f))


def _names_in(text: str) -> list[str]:
    out = [m.group(0) for m in NAME_RE.finditer(text)]
    out += [m.group(0) for m in PSEUDO_RE.finditer(text)]
    return out


def build_index(results: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """All names and numbers that appear in the tool results of this question."""
    names: set[str] = set()
    nums: set[str] = set()
    for r in results:
        for v in _walk(r):
            if isinstance(v, tuple) and v[0] == "__len__":
                nums.add(str(v[1]))  # count of a returned list (e.g. 2 entry points)
                continue
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float)):
                nums.add(_norm_num(v))
                continue
            s = str(v)
            for n in _names_in(s):
                names.add(n)
                if ":" in n:
                    names.add(n.split(":", 1)[1])
            # keys like TABL:ZS4_IOT_DATA also ground the bare name
            for part in re.split(r"[\s>\-,;()\[\]]+", s):
                if ":" in part:
                    names.add(part.split(":", 1)[1])
            for m in NUM_RE.finditer(s):
                nums.add(_norm_num(m.group(0)))
    return names, nums


def check(texts: dict[str, str], results: list[dict[str, Any]], evidence_used: list[tuple[str, str]],
          valid_evidence: set[str]) -> dict[str, Any]:
    """texts: {"answer": ..., "claim 1": ...}. evidence_used: [(where, evidence_id)]."""
    names, nums = build_index(results)
    flags: list[dict[str, str]] = []
    checked_names = checked_nums = 0
    for where, text in texts.items():
        stripped = text
        for n in sorted(set(_names_in(text)), key=len, reverse=True):
            stripped = stripped.replace(n, " ")
            bare = n.split(":", 1)[1] if ":" in n else n
            if n in IGNORE_NAMES:
                continue
            checked_names += 1
            if n not in names and bare not in names:
                flags.append({"token": n, "kind": "name", "where": where,
                              "why": "not in this question's Graphene results"})
        for m in NUM_RE.finditer(stripped):
            tok = m.group(0)
            checked_nums += 1
            if _norm_num(tok) not in nums:
                flags.append({"token": tok, "kind": "number", "where": where,
                              "why": "number not in this question's Graphene results"})
    for where, ev in evidence_used:
        if ev not in valid_evidence:
            flags.append({"token": ev, "kind": "evidence", "where": where,
                          "why": "evidence id not returned by Graphene for this question"})
    return {
        "ok": not flags,
        "checked_names": checked_names,
        "checked_numbers": checked_nums,
        "flags": flags,
    }
