from app import grounding

RES = [{"result": {"key": "TABL:ZS4_IOT_DATA", "total_affected": 9, "score": 0.47, "percentile": 0.8833,
                   "entry_points": ["FUNC:A_1", "FUNC:B_2"], "text": "runs daily, 12 runs in window",
                   "job": "JOB:JOB-5192e9d4d2ab"}, "evidence_ids": ["node:TABL:ZS4_IOT_DATA"]}]


def check(text, ev=()):
    return grounding.check({"answer": text}, RES, [("answer", e) for e in ev], {"node:TABL:ZS4_IOT_DATA", "call:1"})


def test_grounded_text_passes():
    g = check("ZS4_IOT_DATA affects 9 objects; score 0.470 at percentile 0.8833. 2 entry points: "
              "A_1 and FUNC:B_2. JOB-5192e9d4d2ab runs daily, 12 runs in window.", ["node:TABL:ZS4_IOT_DATA", "call:1"])
    assert g["ok"], g["flags"]


def test_recomputed_percentile_is_flagged():
    g = check("It is in the 88th percentile.")
    assert [f["token"] for f in g["flags"]] == ["88"]


def test_unknown_object_number_and_evidence_flagged():
    g = check("ZS4_IOT_DELETE_DATA affects 10 objects.", ["edge:nope"])
    assert {f["token"] for f in g["flags"]} == {"ZS4_IOT_DELETE_DATA", "10", "edge:nope"}


def test_pseudonym_must_be_in_results():
    g = check("Job JOB-deadbeef01 runs it.")
    assert [f["token"] for f in g["flags"]] == ["JOB-deadbeef01"]


def test_graphene_percentile_label_is_grounded():
    res = [{"result": {"percentile": 0.8833, "percentile_label": "88th percentile"}}]
    g = grounding.check({"answer": "It is at the 88th percentile."}, res, [], set())
    assert g["ok"], g["flags"]


def test_dates_and_times_checked_as_whole_tokens():
    res = [{"result": {"history": [{"assessed_at": "2026-11-12T13:06:32+00:00", "score": 0.345}]}}]
    ok = grounding.check({"answer": "0.345 on 2026-11-12 at 13:06 (2026-11-12 13:06:32)."}, res, [], set())
    assert ok["ok"], ok["flags"]
    bad = grounding.check({"answer": "0.345 on 2026-11-13 at 13:07."}, res, [], set())
    assert {f["token"] for f in bad["flags"]} == {"2026-11-13", "13:07"}


def test_iso_timestamp_with_offset_is_one_token():
    """make eval 2026-11-16: '2026-11-12T14:44:40+00:00' in an answer flagged '00:00' as a time."""
    res = [{"result": {"previous": {"assessed_at": "2026-11-12T14:44:40+00:00"}},
            "freshness": {"L1": {"last_success": "2026-11-10T10:45:44.258476+00:00"}}}]
    ok = grounding.check({"answer": "Last stored 2026-11-12T14:44:40+00:00; L1 learned "
                                    "2026-11-10T10:45:44.258476+00:00 (2026-11-10 10:45)."}, res, [], set())
    assert ok["ok"], ok["flags"]
    bad = grounding.check({"answer": "Last stored 2026-11-12T14:44:41+00:00."}, res, [], set())
    assert {f["token"] for f in bad["flags"]} == {"2026-11-12T14:44:41+00:00"}


def test_time_followed_by_prose_colon():
    """make eval 2026-11-16: '- 2026-11-12 13:06:32: 0.3225 to 0.345' flagged '13' as a number."""
    res = [{"result": {"history": [{"assessed_at": "2026-11-12T13:06:32+00:00", "score": 0.345},
                                   {"assessed_at": "2026-11-12T12:53:47+00:00", "score": 0.3225}]}}]
    ok = grounding.check({"answer": "- 2026-11-12 13:06:32: 0.3225 to 0.345\n- at 12:53: 0.3225"}, res, [], set())
    assert ok["ok"], ok["flags"]


def test_self_talk_is_a_wording_problem():
    """Operator 2026-11-18: the assistant never talks about itself or how it knows."""
    from app.wording import problems
    for bad in ("As I learned it, your kernel is 7.53.", "My kernel data says patch 700.",
                "Since the last learning run nothing changed.", "### What I don't know",
                "I was trained on your system.", "The engine returned 9 objects.", "Graphene's tools show it."):
        assert problems(bad), bad
    for ok in ("I don't have the system log for this system; you can check it in SM21.",
               "### Worth checking", "Kernel details as of 5 November; confirm in System → Status.",
               "I'm here to help with your SAP system; what would you like to check?"):
        assert problems(ok) == [], ok


def test_dates_in_words_are_checked_like_iso_dates():
    r = [{"result": {"last_run": "2026-11-05T14:26:30+00:00", "kernel": [{"release": "753", "patch_level": "700"}]}}]
    ok = grounding.check_system("Kernel details as of 5 November; confirm in System → Status.", [], r, [], [], set())
    assert ok["ok"], ok
    bad = grounding.check_system("Kernel details as of 6 November.", [], r, [], [], set())
    assert [f["token"] for f in bad["flags"]] == ["6 November"]



def test_escaped_newlines_become_line_breaks():
    """eval 2026-11-18: the model wrote "\\n\\n" as text in a note answer."""
    from app.wording import fix_escaped_newlines as f
    assert f("Yes.\\n\\n- Update the kernel.") == "Yes.\n\n- Update the kernel."
    assert f("Use `a\\nb` here.") == "Use `a\\nb` here."
    assert f("No escapes.\nFine.") == "No escapes.\nFine."


def test_numeric_dates_and_gaps_labels_are_wording_problems():
    """Operator 2026-11-18: dates in words only; "Not available", never a "Gaps" label."""
    from app.wording import problems
    for bad in ("4305 dumps from 2026-10-29 to 2026-11-11.", "Released on 2026.11.10.", "Since 29.10.2026.",
                "Gaps:\n- SM21", "**Gaps:** SM21", "Main gaps: SM21"):
        assert problems(bad), bad
    for ok in ("4305 dumps from 29 October to 11 November.", "**Not available:** the system log (check SM21).",
               "The gaps are small.", "SAP_BASIS 751, kernel 7.53 (release 753)."):
        assert problems(ok) == [], ok



def test_exact_derivations_with_their_source_numbers_are_backed():
    """Operator 2026-11-18: plain arithmetic on engine numbers, the source number in the same sentence."""
    r = [{"result": {"jobs": {"runs": 42}, "dumps": {"total": 4034, "per_day": {"2026-11-04": 288}},
                     "run": {"days": ["d"] * 14}}}]
    ok = ["This system had 4,034 short dumps in total.",
          "This system logs 288 dumps a day, which is one every 5 minutes.",
          "This system had 42 job runs in 14 days, so 3 a day.",
          "This system logs 288 dumps a day, 12 an hour."]
    for t in ok:
        c = grounding.check_system(t, [], r, [], [], set())
        assert c["ok"], (t, c["flags"])
    derived = grounding.check_system(ok[1], [], r, [], [], set())["derived"]
    assert [d["token"] for d in derived] == ["5"]  # logged for ?debug=1
    bad = {"This system had 4034 short dumps, 17 of them today.": "17",      # not derivable
           "This system: one every 5 minutes.": "5",                          # no source number alongside
           "This system logs 288 dumps a day, one every 4 minutes.": "4",     # wrong arithmetic
           "This system had 4,035 short dumps in total.": "4,035"}
    for t, tok in bad.items():
        c = grounding.check_system(t, [], r, [], [], set())
        assert tok in [f["token"] for f in c["flags"]], (t, c)



def test_eval_counts_either_sentences_as_gap_mentions():
    """Operator 2026-11-18: the one-sentence closing check also catches "… either"."""
    import importlib.util, pathlib, re as _re
    spec = importlib.util.spec_from_file_location("run_eval", pathlib.Path(__file__).resolve().parents[1] / "eval/run_eval.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    closing = ("Which program each dump hit isn't recorded here, and neither is the system log. Check the system log "
               "in SM21. CCMS isn't configured on this system either.")
    sentences = [x for x in _re.split(r"(?<=[.!?])\s+", closing) if x]
    assert [bool(m.GAP_RE.search(x)) for x in sentences] == [True, False, True]
    one = "Which program each dump hit and the system log aren't recorded here; check them in ST22 and SM21."
    assert m.GAP_RE.search(one) and not m.GAP_RE.search("Check the system log in SM21.")



def _run_eval_module():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("run_eval", pathlib.Path(__file__).resolve().parents[1] / "eval/run_eval.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_gap_detector_skips_steps_and_recommendations():
    """Operator 2026-11-18: a gap turned into a recommendation (SM36 → Standard Jobs) or inside a numbered
    step is not a gap mention; standalone gap sentences still count."""
    m = _run_eval_module()
    monitoring = ("Next steps:\n3. Workload statistics aren't collected, which usually means the standard jobs aren't "
                  "scheduled. In SM36 use Standard Jobs.\n\nWhich program each dump hit isn't recorded here, and neither "
                  "is the system log; check the system log in SM21.")
    assert len(m.gap_sentences(monitoring)) == 1
    sm21 = ("CCMS isn't configured and there are no alerts. Workload statistics aren't being collected, so SM36 → "
            "Standard Jobs (including SAP_COLLECTOR_FOR_PERFMONITOR) is worth checking. The system log content itself "
            "isn't recorded for this system.")
    assert len(m.gap_sentences(sm21)) == 1
    two = ("Which program each dump hit isn't recorded here, and neither is the system log. Check the system log in SM21. "
           "CCMS isn't configured on this system either.")
    assert len(m.gap_sentences(two)) == 2


def test_tag_rule_style_only_vs_substance():
    m = _run_eval_module()
    style = {"ok": False, "id": "a", "problems": ["answer has 153 words > 140"]}
    gap = {"ok": False, "id": "b", "problems": ["gap mentioned in 2 sentences > 1: ['x', 'y']"]}
    fact = {"ok": False, "id": "c", "problems": ["answer lacks ['1616']"]}
    ok = {"ok": True, "id": "d", "problems": []}
    assert m.tag_verdict([style, gap, ok]).startswith("TAG OK")
    assert m.tag_verdict([style, gap, dict(style, id="e"), dict(gap, id="f")]).startswith("NO TAG: 4 style-only")
    assert m.tag_verdict([style, fact]).startswith("NO TAG: substance failures ['c']")
    assert m.tag_verdict([{"ok": False, "id": "g", "error": "timeout"}]).startswith("NO TAG")



def test_compact_sap_dates_back_iso_and_words():
    """eval 2026-11-18: find_object's last_changed "20161027" backs "27 October 2016"."""
    r = [{"result": {"objects": [{"name": "ZS4_IOT_DATA", "last_changed": "20161027"}]}}]
    for t in ("ZS4_IOT_DATA was last changed on 27 October 2016.", "ZS4_IOT_DATA was last changed on 2016-10-27."):
        assert grounding.check_system(t, [t], r, [], [], set())["ok"], t
    assert not grounding.check_system("ZS4_IOT_DATA was last changed on 28 October 2016.", ["changed 28 October 2016"],
                                      r, [], [], set())["ok"]


def test_product_name_check_ignores_upper_case_sap_text():
    from app.wording import problems
    assert problems("Graphene says so.") and problems("graphene says so.")
    assert problems('Its description is "GRAPHENE DEMO - IoT data change".') == []



def test_full_eval_cap_scales_with_questions():
    """Operator 2026-11-18: $0.065 a question, rounded up to the next 10 cents."""
    m = _run_eval_module()
    assert m.full_cap(28) == 1.9 and m.full_cap(23) == 1.5 and m.full_cap(20) == 1.3 and m.full_cap(10) == 0.7


def test_held_back_answer_still_reads_cleanly():
    """Operator 2026-11-18: no orphaned sentences, steps renumbered, no empty lead-in after a hold-back."""
    from app.present import _drop_tokens
    md = ("Dumps are counted per day: 4,034 short dumps in total.\n\n"
          "Nothing links them to ZS4_IOT_X. Those are the places a dump would show up.\n\n"
          "To check:\n1. In ST22, read the runtime error.\n2. Filter by ZS4_IOT_X in ST22.\n3. Check the table in SE11.\n"
          "4. Compare the timing in SM37.\n\nWant me to look at one of those objects?")
    out, n = _drop_tokens(md, {"ZS4_IOT_X"})
    assert "ZS4_IOT_X" not in out and "Those are the places" not in out and n == 3
    assert "1. In ST22, read the runtime error.\n2. Check the table in SE11.\n3. Compare the timing in SM37." in out
    assert "To check:" in out and "\n\n\n" not in out
    only_steps_gone, _ = _drop_tokens("To check:\n1. Filter by ZS4_IOT_X.\n\nWant me to help?", {"ZS4_IOT_X"})
    assert "To check:" not in only_steps_gone and only_steps_gone.startswith("Want me to help?")
    kept, n = _drop_tokens("This system had 4 dumps. That is fine.", {"ZS4_IOT_X"})
    assert kept == "This system had 4 dumps. That is fine." and n == 0  # back-references stay when nothing was dropped


def test_soft_length_limits():
    """Operator 2026-11-18: up to 20% over a length/sentence limit warns; above fails."""
    m = _run_eval_module()
    p, n = [], []
    m.soft_limit(168, 140, "words", p, n)
    assert p == [] and n and n[0].startswith("warning: 168 words > 140")
    p, n = [], []
    m.soft_limit(169, 140, "words", p, n)
    assert p == ["answer has 169 words > 140"]
    p, n = [], []
    m.soft_limit(3, 2, "sentences", p, n)
    assert p == ["answer has 3 sentences > 2"]  # 50% over



def test_sap_namespaces_are_general_knowledge():
    """Operator 2026-11-18: /IWFND/ERROR_LOG is SAP's Gateway error log, not an object of this system."""
    r = [{"result": {"x": 1}}]
    assert grounding.check_system("Check /IWFND/ERROR_LOG and /UI2/FLP in your system.", [], r, [], [], set())["ok"]
    assert not grounding.check_system("Your system has /ACME/ORDERS.", [], r, [], [], set())["ok"]


def test_latency_soft_once_blocking_twice_or_above_15s():
    m = _run_eval_module()
    case = {"expect": {}}
    out = {"presentation": {"outcome": "passed"}, "tool_trace": [], "answer": "Want me to check it?",
           "system_facts": [], "history": None}
    p, n = m.check(case, out, "Want me to check it?", 13.2)
    assert not any(x.startswith("slow") for x in p) and any(x.startswith("warning: slow") for x in n)
    p, n = m.check(case, out, "Want me to check it?", 15.4)
    assert "slow: 15.4 s > 10.0 s" in p
    one = {"id": "a", "ok": True, "problems": [], "notes": ["warning: slow 13.2 s > 10.0 s (up to 15 s)"]}
    assert m.tag_verdict([one, {"id": "b", "ok": True, "problems": [], "notes": []}]).startswith("TAG OK")
    assert m.tag_verdict([one, dict(one, id="c")]).startswith("NO TAG: 2 cases over their time limit")
