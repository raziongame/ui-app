"""The chat UI (operator 2026-11-17): Graphene by NiraamAI on /, like ChatGPT, plain and simple;
chats stored on the server per browser; NiraamAI colours with WCAG AA text."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, ask

HTML = (ROOT / "app" / "static" / "index.html").read_text()


def _lum(h: str) -> float:
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def palette(mode: str) -> dict[str, str]:
    block = re.search(r":root \{(.*?)\}", HTML, re.S).group(1) if mode == "light" else \
        re.search(r':root\[data-theme="dark"\] \{(.*?)\}', HTML, re.S).group(1)
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})", block))


def test_one_brand_on_root(client):
    html = client.get("/").text
    assert "<title>Graphene</title>" in html and 'src="/brand/logo.png"' in html
    assert "How can I help with your SAP system?" in html
    for gone in ("Cloudbox", "/direct", "What I've learned", "How do I know", "Connected to", "chips",
                 "sample partner app", "grounding"):
        assert gone not in html, gone
    assert client.get("/direct").status_code == 404
    assert client.get("/api/learned").status_code == 404


def test_composer_enter_sends_shift_enter_newline_long_pastes():
    assert 'e.key === "Enter" && !e.shiftKey' in HTML and 'maxlength="200000"' in HTML
    assert "New chat" in HTML and "Delete chat" in HTML
    from app.server import MAX_MESSAGE_CHARS
    assert MAX_MESSAGE_CHARS >= 50_000


def test_niraamai_colours():
    light, dark = palette("light"), palette("dark")
    assert light["heading"] == "#003858" and light["button-bg"] == "#2A82B4" and light["accent"] == "#1298B0"
    assert light["focus"] == "#4AC2E0" and dark["bg"] == "#0B1C2A"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_colours_meet_wcag_aa(mode):
    c = palette(mode)
    for fg, bg in (("text", "bg"), ("text", "sidebar"), ("text", "bubble"), ("text", "code-bg"), ("muted", "bg"),
                   ("muted", "sidebar"), ("heading", "bg"), ("link", "bg"), ("link", "bubble")):
        assert contrast(c[fg], c[bg]) >= 4.5, (mode, fg, bg, round(contrast(c[fg], c[bg]), 2))
    assert contrast(c["button-text"], c["button-bg"]) >= 3.0  # the send arrow: large, bold
    assert contrast(c["accent"], c["bg"]) >= 3.0  # marks (thinking dot)


def test_logo_is_served(client):
    r = client.get("/brand/logo.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/brand/..%2Fapp%2Fconfig.py").status_code == 404


def test_chats_are_per_browser_and_can_be_deleted(client):
    out, _ = ask(client, "What uses table ZFAKE_TABLE?")
    cid = out["chat_id"]
    mine = client.get("/api/chats").json()["chats"]
    assert cid in [c["id"] for c in mine]
    assert next(c for c in mine if c["id"] == cid)["title"] == "What uses table ZFAKE_TABLE?"
    other = TestClient(client.app)  # another browser: no cookie, so a new id
    assert cid not in [c["id"] for c in other.get("/api/chats").json()["chats"]]
    assert other.get(f"/api/chats/{cid}").status_code == 404
    assert other.delete(f"/api/chats/{cid}").status_code == 404
    assert other.post("/api/ask", json={"message": "hi", "chat_id": cid}).status_code == 404
    assert client.delete(f"/api/chats/{cid}").status_code == 200
    assert client.get(f"/api/chats/{cid}").status_code == 404
    assert cid not in [c["id"] for c in client.get("/api/chats").json()["chats"]]


def test_cookie_is_httponly():
    from app.server import set_cookie
    from fastapi import Response
    r = Response()
    set_cookie(r, "0" * 32)
    h = r.headers["set-cookie"].lower()
    assert "httponly" in h and "samesite=lax" in h


def test_chat_title_from_a_pasted_note():
    from app.store import title_for
    note = (ROOT / "tests" / "fixtures" / "note_3778530_excerpt.txt").read_text() + "\n\nIs this relevant to our system?"
    assert title_for(note) == "Is this relevant to our system?"
    assert title_for("What breaks if I change ZS4_IOT_DATA?") == "What breaks if I change ZS4_IOT_DATA?"
    assert len(title_for("x" * 300)) == 60


def test_input_has_no_inner_focus_outline():
    """Operator 2026-11-18: only the rounded outer border highlights on focus."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text()
    assert "textarea:focus, textarea:focus-visible { outline: none; box-shadow: none; }" in html
    assert "form:focus-within { border-color: var(--focus); }" in html


def test_user_bubble_trims_blank_lines_for_display_only():
    """Operator 2026-11-18: trim leading/trailing blank lines, collapse 3+ blank lines into one; the
    model still gets the original text."""
    import re as _re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app/static/index.html").read_text()
    assert "b.textContent = bubbleText(text)" in html
    assert 'body: JSON.stringify({message: text' in html or "message: text" in html

    def bubble(t):  # the same rules as bubbleText() in the page
        t = t.replace("\r\n", "\n")
        t = _re.sub(r"^(?:[ \t]*\n)+", "", t)
        t = _re.sub(r"(?:\n[ \t]*)+$", "", t)
        return _re.sub(r"\n(?:[ \t]*\n){3,}", "\n\n", t)
    assert bubble("\n\n  \nNote 123\n\n\n\n\nIs this relevant?\n\n") == "Note 123\n\nIs this relevant?"
    assert bubble("a\n\nb") == "a\n\nb" and bubble("a\n\n\nb") == "a\n\n\nb"
