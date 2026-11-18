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
