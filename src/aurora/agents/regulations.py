"""Índice do regulamento interno por capítulos + busca por termos.

Sem dependências externas: os capítulos são extraídos do markdown uma única
vez e pontuados por sobreposição de tokens com a consulta (decisão D6).
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..config import REGULATIONS_MD

STOPWORDS = {
    "a", "o", "e", "de", "da", "do", "del", "á", "com", "que", "para", "por",
    "se", "en", "no", "na", "los", "las", "el", "al", "um", "uma", "una",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-záéíóúñçàèìòù]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


@lru_cache(maxsize=1)
def _chapters() -> list[dict[str, str]]:
    text = REGULATIONS_MD.read_text(encoding="utf-8")
    lines = text.splitlines()
    chapters: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                chapters.append(current)
            current = {"title": line[3:].strip(), "body": ""}
            continue
        if current is not None:
            current["body"] += line + "\n"
    if current is not None:
        chapters.append(current)
    return [{**c, "tokens": _tokens(c["title"] + "\n" + c["body"])} for c in chapters]


def search(query: str, max_chapters: int = 2, max_chars: int = 1200) -> str:
    """Retorna os capítulos mais relevantes para a consulta (formato curto).

    - Nenhuma coincidência -> mensagem de aviso (não inventar regras).
    - Coincidências -> máximo `max_chapters` capítulos, recortados.
    """
    q = _tokens(query)
    chapters = _chapters()
    if not chapters:
        return "Não consegui localizar essa informação no regulamento."
    ranked = sorted(
        ((len(q & c["tokens"]), c) for c in chapters), key=lambda x: -x[0]
    )
    best = [c for score, c in ranked if score > 0][:max_chapters]
    if not best:
        return ("Não há nenhuma regra específica sobre isso no regulamento. "
                "Como regra geral aplica o Capítulo I (Disposições gerais).")
    excerpts = []
    for c in best:
        body = c["body"].strip()
        excerpts.append(f"### {c['title']}\n{body[:max_chars]}")
    return "\n\n".join(excerpts[:max_chapters])