"""Lexical term extraction for graph keyword search (no embeddings)."""

from __future__ import annotations

import re
import unicodedata


def strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def terms_from_question(question: str) -> list[str]:
    """
    Split a natural-language question into search terms that overlap graph labels/paths.

    Handles camelCase / PascalCase so tokens like ``OAuthFlow`` become ``oauth``, ``flow``.
    """
    if not (question or "").strip():
        return []
    s = strip_diacritics(question)
    # Split camelCase / PascalCase boundaries (e.g. getUserID -> get User ID)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    parts = re.findall(r"[A-Za-z0-9]+", s)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = p.lower()
        if len(t) < 2:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out
