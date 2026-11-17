"""White-label skins (brands.yaml) and the wording rules every skin enforces.

The engine (Graphene, over MCP) is the same behind every skin. A skin with product_name_allowed: false
never shows the engine's product name: not in the page, answers, sources, progress or error messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from . import config

ENGINE_PRODUCT_NAME = "Graphene"

# Operator 2026-11-17: "learned", never "trained" / "fine-tuned" / "our own AI model" (any skin).
FORBIDDEN_WORDING_RE = re.compile(r"\b(?:trained|training|fine[- ]?tuned|fine[- ]?tuning|our own AI model)\b", re.I)
# Engine internals that never reach a skin: tool names, protocol, evidence/call ids.
TOOL_NAMES = ("find_object", "assess_transport", "system_profile", "impact", "status", "operations")
INTERNAL_RE = re.compile(r"\b(?:find_object|assess_transport|system_profile|MCP|call:\d+|tool_trace|evidence_ids?)\b")

FALLBACK_ANSWER = "I'm not certain about that one. Can you rephrase or name a specific object?"
NOT_READY_ANSWER = "I'm still connecting to your SAP system. Please ask again in a moment."
ERROR_ANSWER = "I couldn't answer that just now. Please try again."


@dataclass(frozen=True)
class Brand:
    key: str
    path: str
    name: str
    wordmark: tuple[str, ...] | None
    tagline: str
    logo_text: str
    logo_image: str | None
    byline: str | None
    product_name_allowed: bool
    colors: dict[str, dict[str, str]]

    def public(self) -> dict[str, Any]:
        """What the page gets (no internals)."""
        return {"key": self.key, "name": self.name, "wordmark": list(self.wordmark) if self.wordmark else None,
                "tagline": self.tagline, "logo_text": self.logo_text,
                "logo_image": f"/brand/{self.logo_image}" if self.logo_image else None, "byline": self.byline}


@lru_cache(maxsize=1)
def load() -> dict[str, Brand]:
    raw = yaml.safe_load((config.ROOT / "brands.yaml").read_text())
    return {k: Brand(key=k, path=v["path"], name=v["name"],
                     wordmark=tuple(v["wordmark"]) if v.get("wordmark") else None, tagline=v.get("tagline", ""),
                     logo_text=v.get("logo_text") or "", logo_image=v.get("logo_image"), byline=v.get("byline"),
                     product_name_allowed=bool(v.get("product_name_allowed")), colors=v["colors"])
            for k, v in raw.items()}


def get(key: str | None) -> Brand:
    brands = load()
    return brands.get(key or "partner") or brands["partner"]


def wording_problems(text: str, brand: Brand) -> list[str]:
    """Why this text may not be shown in this skin (empty = fine)."""
    out = [f"wording: {m.group(0)}" for m in FORBIDDEN_WORDING_RE.finditer(text)]
    out += [f"internal: {m.group(0)}" for m in INTERNAL_RE.finditer(text)]
    if not brand.product_name_allowed and ENGINE_PRODUCT_NAME.lower() in text.lower():
        out.append("product name in a white-label skin")
    return out
