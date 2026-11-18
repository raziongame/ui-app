"""Grounding check (operator 2026-11-17: full SAP assistant). General SAP knowledge is allowed;
only statements about THIS system are checked. Their names, numbers and dates must appear in this
chat's engine results or in the user's own messages. Anything else is flagged; the agent then
rewrites the answer once, and what is still unbacked is held back. Flags go to the log only.
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
# "4,034" (thousands separators) is one number (operator 2026-11-18: "4,034 short dumps in total").
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.:/-])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![0-9])")
# Dates and times are checked as whole tokens: 2026-11-12, 2026-11-12 13:06, 13:06, 13:06:32,
# 2026-11-12T14:44:40+00:00 (fraction and UTC offset belong to the token, never a separate "00:00").
DATETIME_RE = re.compile(r"(?<![\d-])(\d{4}-\d{2}-\d{2})"
                         r"(?:[T ](\d{2}:\d{2})(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?(?!\d|:\d)")
TIME_RE = re.compile(r"(?<![\d:])\d{2}:\d{2}(?::\d{2})?(?!\d|:\d)")  # "13:06: text" is a time
# Operator 2026-11-18: a date in words ("kernel details as of 5 November") is checked as a whole date.
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
_MON = r"(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + "|" + "|".join(m[:3] + r"\.?" for m in MONTHS) + ")"
NATURAL_DATE_RE = re.compile(rf"(?<![\w-])(?:\d{{1,2}}\s+{_MON}(?:,?\s+\d{{4}})?|{_MON}\s+\d{{1,2}}(?:,?\s+\d{{4}})?)(?![\w-])")


def _natural_forms(d: str) -> set[str]:
    y, m, day = d.split("-")
    full = MONTHS[int(m) - 1]
    out: set[str] = set()
    for mon in {full, full[:3], full[:3] + "."}:
        dd = str(int(day))
        out |= {f"{dd} {mon}", f"{dd} {mon} {y}", f"{mon} {dd}", f"{mon} {dd}, {y}", f"{mon} {dd} {y}"}
    return out

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
    f = float(s.replace(",", "")) if isinstance(s, str) else float(s)
    return repr(f) if f != int(f) else str(int(f))


def _names_in(text: str) -> list[str]:
    out = [m.group(0) for m in NAME_RE.finditer(text)]
    out += [m.group(0) for m in PSEUDO_RE.finditer(text)]
    return out


def _datetime_forms(s: str) -> set[str]:
    """Every way an answer may quote a timestamp Graphene returned (date, time, date + time)."""
    out: set[str] = set()
    # dates in words and times as a result writes them (e.g. a kernel build date "Sep  1 2020 21:43:19")
    out |= {re.sub(r"\s+", " ", m.group(0)) for m in NATURAL_DATE_RE.finditer(s)}
    out |= {m.group(0) for m in TIME_RE.finditer(s)}
    for m in DATETIME_RE.finditer(s):
        d, hm, sec, frac, tz = m.group(1), m.group(2), m.group(3) or "", m.group(4) or "", m.group(5) or ""
        out.add(d)
        out |= _natural_forms(d)
        if hm:
            out |= {hm, hm + sec, f"{d} {hm}", f"{d} {hm}{sec}", f"{d}T{hm}{sec}", m.group(0)}
            if tz:
                out |= {f"{d}T{hm}{sec}{tz}", f"{d} {hm}{sec}{tz}"}
    return out


def build_index(results: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    """All names, numbers and dates/times that appear in the tool results of this question."""
    names: set[str] = set()
    nums: set[str] = set()
    dates: set[str] = set()
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
            dates |= _datetime_forms(s)
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
    return names, nums, dates


def check(texts: dict[str, str], results: list[dict[str, Any]], evidence_used: list[tuple[str, str]],
          valid_evidence: set[str]) -> dict[str, Any]:
    """texts: {"answer": ..., "claim 1": ...}. evidence_used: [(where, evidence_id)]."""
    names, nums, dates = build_index(results)
    flags: list[dict[str, str]] = []
    checked_names = checked_nums = 0
    for where, text in texts.items():
        stripped = text
        for rx in (DATETIME_RE, NATURAL_DATE_RE, TIME_RE):
            for m in list(rx.finditer(stripped)):
                tok = re.sub(r"\s+", " ", m.group(0))
                checked_nums += 1
                if tok not in dates:
                    flags.append({"token": tok, "kind": "date", "where": where,
                                  "why": "date/time not in this question's Graphene results"})
            stripped = rx.sub(" ", stripped)
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


# ---- system facts (operator 2026-11-17) --------------------------------------------------------
# Names that only exist in one system: customer objects (Z*, Y*, /NAMESPACE/), transports (<SID>K<6
# digits>) and pseudonyms. An answer that names one must be backed even outside a declared system fact.
# Z/Y names need a digit or underscore (so "YES" in a table is not an object) unless typed (PROG:ZPRICE).
SYSTEM_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_/])(?:[A-Z]{2,5}:[ZY][A-Z0-9_]{2,}|(?=[ZY][A-Z]*[_0-9])[ZY][A-Z0-9_]{2,}"
    r"|/[A-Z0-9_]+/[A-Z0-9_]+|[A-Z0-9]{3}K\d{6})(?![A-Za-z0-9_])")
# Sentences that speak about this system: their numbers and dates must be backed too.
SYSTEM_MARKER_RE = re.compile(
    r"\b(?:your|our)\s+(?:SAP\s+)?(?:system|kernel|landscape|release|version|patch|database|server|instance)s?\b"
    r"|\bthis system\b|\byou(?:'re| are)\s+(?:on|running)\b|\byou run\b|\bas of\b", re.I)


# Operator 2026-11-18: plain arithmetic on engine numbers is allowed when the source number is stated
# alongside ("288 a day is one every 5 minutes", "42 job runs in 14 days is 3 a day"). A number that
# isn't in the results is backed if it is a + b, a - b, a × b, a ÷ b (or a ÷ b × 100) of numbers in the
# SAME sentence that are backed, or of one of them and a time constant; at least one operand must be
# a backed number of that sentence; the result must match to the precision written.
TIME_CONSTANTS = (7.0, 24.0, 60.0, 1440.0)


def _derivable(tok: str, operands: list[float]) -> bool:
    v = float(tok.replace(",", ""))
    dec = len(tok.split(".", 1)[1]) if "." in tok else 0
    pool = [(x, True) for x in operands] + [(c, False) for c in TIME_CONSTANTS]
    for a, a_src in pool:
        for b, b_src in pool:
            if not (a_src or b_src):
                continue
            outs = [a + b, a - b, a * b]
            if b:
                outs += [a / b, a / b * 100]
            if any(abs(round(o, dec) - v) < 1e-9 for o in outs):
                return True
    return False


def kernel_display_forms(results: list[dict[str, Any]]) -> set[str]:
    """Operator 2026-11-17: "kernel 7.53 (release 753)". The engine returns the kernel release as
    "753"; its dotted form 7.53 (as people read it) is backed by it. Kernel releases only: a SAP_BASIS
    release stays as returned (751, never 7.51)."""
    out: set[str] = set()
    for r in results:
        res = (r or {}).get("result") or {}
        for k in res.get("kernel") or [] if isinstance(res.get("kernel"), list) else []:
            rel = str((k or {}).get("release", ""))
            if re.fullmatch(r"\d{3}", rel):
                out.add(f"{rel[0]}.{rel[1:]}")
    return out


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for line in text.splitlines():
        parts += [p for p in re.split(r"(?<=[.!?])\s+|\s*\|\s*", line) if p.strip()]
    return parts


def check_system(answer: str, facts: list[str], results: list[dict[str, Any]], user_texts: list[str],
                 evidence_used: list[tuple[str, str]], valid_evidence: set[str]) -> dict[str, Any]:
    """Check what an answer says about this system. facts: the declared system facts (checked in
    full); the answer text: system-only names everywhere, numbers/dates in sentences about this system."""
    names, nums, dates = build_index(results + [{"user": t} for t in user_texts])
    for f in kernel_display_forms(results):
        nums.add(_norm_num(f))
    flags: list[dict[str, str]] = []
    derived: list[dict[str, str]] = []  # plain arithmetic on backed numbers (logged, ?debug=1)
    checked = 0

    def check_numbers_dates(text: str, where: str) -> str:
        nonlocal checked
        stripped = text
        for rx in (DATETIME_RE, NATURAL_DATE_RE, TIME_RE):
            for m in list(rx.finditer(stripped)):
                checked += 1
                if re.sub(r"\s+", " ", m.group(0)) not in dates:
                    flags.append({"token": m.group(0), "kind": "date", "where": where})
            stripped = rx.sub(" ", stripped)
        for n in sorted(set(_names_in(text)), key=len, reverse=True):
            stripped = stripped.replace(n, " ")  # digits inside names are not numbers
        toks = [m.group(0) for m in NUM_RE.finditer(stripped)]
        backed = [float(t.replace(",", "")) for t in toks if _norm_num(t) in nums]
        for t in toks:
            checked += 1
            if _norm_num(t) in nums:
                continue
            others = [x for x in backed if x != float(t.replace(",", ""))]
            if _derivable(t, others):
                derived.append({"token": t, "where": where})
                continue
            flags.append({"token": t, "kind": "number", "where": where})
        return stripped

    def check_names(text: str, where: str, rx: re.Pattern) -> None:
        nonlocal checked
        for m in rx.finditer(text):
            n = m.group(0)
            bare = n.split(":", 1)[1] if ":" in n else n
            checked += 1
            if n not in names and bare not in names:
                flags.append({"token": n, "kind": "name", "where": where})

    for i, f in enumerate(facts, start=1):
        where = f"system fact {i}"
        check_names(f, where, SYSTEM_NAME_RE)
        check_numbers_dates(f, where)
    check_names(answer, "answer", SYSTEM_NAME_RE)
    for sent in _sentences(answer):
        if SYSTEM_MARKER_RE.search(sent):
            check_numbers_dates(sent, "answer")
    for where, ev in evidence_used:
        if ev not in valid_evidence:
            flags.append({"token": ev, "kind": "evidence", "where": where})
    seen, uniq = set(), []
    for fl in flags:
        key = (fl["token"], fl["kind"], fl["where"])
        if key not in seen:
            seen.add(key)
            uniq.append(fl)
    return {"ok": not uniq, "checked": checked, "flags": uniq, "derived": derived}
