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
