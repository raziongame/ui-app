"""White-label skins (operator 2026-11-17): two brands, one engine. The partner skin never shows the
engine's product name; both skins hide internals; silent safety replaces unbacked answers."""
from __future__ import annotations

import re

import pytest

from app import brand as brands
from app import present

FORBIDDEN_IN_ANY_SKIN = ["sample partner app", "demo by the", "MCP", "evidence panel", "tool_trace", "grounding"]


def _lum(h: str) -> float:
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_partner_page_never_names_the_engine(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "graphene" not in html.lower()
    assert "<title>Cloudbox · SAP Assistant</title>" in html
    for bad in FORBIDDEN_IN_ANY_SKIN:
        assert bad not in html, bad


def test_direct_page_is_graphene_by_niraamai(client):
    html = client.get("/direct").text
    assert "<title>Graphene</title>" in html
    assert '"byline": "by NiraamAI"' in html and '"logo_image": "/brand/logo.png"' in html
    assert "#003858" in html and "#4AC2E0" in html  # NiraamAI colours
    for bad in FORBIDDEN_IN_ANY_SKIN:
        assert bad not in html, bad


def test_brand_assets_only_named_logos(client):
    r = client.get("/brand/logo.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/brand/..%2Fbrands.yaml").status_code == 404
    assert client.get("/brand/other.png").status_code == 404


@pytest.mark.parametrize("key", ["partner", "direct"])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_colours_meet_wcag_aa(key, mode):
    """Operator 2026-11-17: readable on a projector, text at least WCAG AA."""
    c = brands.get(key).colors[mode]
    for fg, bg in (("text", "bg"), ("text", "surface"), ("muted", "bg"), ("muted", "surface"), ("heading", "bg"),
                   ("link", "bg"), ("link", "surface"), ("header_text", "header_bg")):
        assert contrast(c[fg], c[bg]) >= 4.5, (key, mode, fg, bg, round(contrast(c[fg], c[bg]), 2))
    # button labels are bold >= 20px (large text): AA large is 3:1; ticks/status dots are marks: 3:1
    assert contrast(c["button_text"], c["button_bg"]) >= 3.0
    assert contrast(c["tick"], c["bg"]) >= 3.0


def _out(answer, claims=(), flags=()):
    return {"answer": answer, "claims": [{"text": t, "evidence_ids": []} for t in claims],
            "grounding": {"ok": not flags, "flags": list(flags)}, "coverage": [], "freshness": {}, "history": None}


def test_silent_safety_unbacked_name_or_number_is_never_shown():
    flag = {"token": "12", "kind": "number", "where": "answer", "why": "x"}
    v = present.view(_out("Changing ZS4_IOT_DATA affects 12 objects.", flags=[flag]), brands.get("partner"))
    assert v["fallback"] and v["answer"] == "I'm not certain about that one. Can you rephrase or name a specific object?"
    # an evidence-id flag is internal only: the answer is still shown
    ev = {"token": "edge:x", "kind": "evidence", "where": "claim 1", "why": "x"}
    assert not present.view(_out("I know ZS4_IOT_DATA.", flags=[ev]), brands.get("partner"))["fallback"]


def test_product_name_only_in_the_direct_skin():
    out = _out("Graphene has learned 6 of 11 areas.")
    assert present.view(dict(out), brands.get("partner"))["fallback"]
    assert not present.view(dict(out), brands.get("direct"))["fallback"]


@pytest.mark.parametrize("text", ["I was trained on your system.", "My fine-tuned model knows it.",
                                  "Our own AI model learned it.", "I called assess_transport.", "See call:1."])
def test_wording_rules_in_every_skin(text):
    for key in ("partner", "direct"):
        assert present.view(_out(text), brands.get(key))["fallback"], (key, text)


def test_bad_sources_are_left_out_not_shown():
    v = present.view(_out("I know ZS4_IOT_DATA.", claims=["Table ZS4_IOT_DATA has sp_level null.",
                                                          "Table ZS4_IOT_DATA is in package ZS4_IOT."]),
                     brands.get("partner"))
    assert not v["fallback"] and v["sources"] == ["Table ZS4_IOT_DATA is in package ZS4_IOT."]


def test_stage_ids_become_area_names_in_sources():
    out = _out("ok", claims=["New affected job, learned in L5."])
    out["coverage"] = [{"this_answer_uses": {"L5": {"name": "Operations"}}}]
    out["freshness"] = {"L5": {"age": "5 d"}}
    v = present.view(out, brands.get("partner"))
    assert v["sources"] == ["New affected job, learned in Operations."]
    assert v["learned"] == [{"area": "Operations", "age": "5 days"}]


def test_age_words_keeps_the_engines_number():
    assert present.age_words("5 d") == "5 days" and present.age_words("1 d") == "1 day"
    assert present.age_words("286.2 h") == "286.2 hours" and present.age_words("soon") == "soon"


def test_learned_sidebar_from_the_engines_status(client):
    r = client.get("/api/learned").json()
    assert r["connected"] is True and r["sid"] == "ZUZ"
    areas = {a["area"]: a for a in r["areas"]}
    for name in ("System", "Dictionary", "Code", "Code relationships", "Transports", "Operations"):
        assert areas[name]["learned"] is True and re.match(r"^\d+(\.\d+)? (day|hour|minute|week)s?$", areas[name]["age"])
    for name in ("Configuration", "Access", "Interfaces", "Runtime evidence", "Understanding"):
        assert areas[name]["learned"] is False and areas[name]["age"] == ""
    assert all(set(a) == {"area", "learned", "age"} for a in r["areas"])  # no metrics or percentages
    assert r["last_learned"]
    assert "graphene" not in str(r).lower()
