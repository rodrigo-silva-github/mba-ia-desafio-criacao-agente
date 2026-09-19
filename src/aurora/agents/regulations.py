"""Índice do regulamento interno por capítulos + busca por termos.

Sem dependências externas: os capítulos são extraídos do markdown uma única
vez e pontuados por sobreposição de tokens com a consulta (decisão D6).

O score é ponderado por IDF (token raro vale mais que token comum) e dá peso
dobrado ao TÍTULO do capítulo. Sem isso, um capítulo longo e genérico
("Direitos e deveres dos moradores") vencia consultas sobre assuntos que não
são dele só por ter muitos tokens, e o texto dele acabava anexado ao histórico
— quebrando a Garantia 4 (nenhum evento pode conter trechos de capítulos de
outros assuntos).

Um segundo capítulo só entra se chegar perto do melhor (`min_ratio`): é
preferível responder com um capítulo só a arrastar assunto alheio.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from functools import lru_cache

from ..config import REGULATIONS_MD

STOPWORDS = {
    "a", "o", "e", "de", "da", "do", "del", "com", "que", "para", "por",
    "se", "en", "no", "na", "los", "las", "el", "al", "um", "uma", "una",
    "sua", "seu", "suas", "seus", "como", "mais", "pode", "posso", "qual",
    "quais", "quando", "onde", "sobre", "aos", "das", "dos", "nas", "nos",
    "tem", "ter", "sao", "esta", "este", "isso", "aquilo", "nao", "ate",
    "foi", "ser", "aqui", "la", "ja", "tambem", "apos", "entao",
}

# Vocabulário do morador -> vocabulário do regulamento. Só afeta a BUSCA pelo
# capítulo certo; o texto devolvido continua sendo o do regulamento.
SYNONYMS = {
    "cachorro": ("animais", "caes"),
    "cao": ("animais", "caes"),
    "caes": ("animais",),
    "bicicleta": ("bicicletario", "bicicletas", "garagem"),
    "bicicletas": ("bicicletario",),
    "bike": ("bicicletario", "bicicletas"),
    "barulho": ("silencio", "ruido", "sossego"),
    "carro": ("veiculos", "vagas", "garagem"),
    "festa": ("salao", "festas"),
    "churrasco": ("churrasqueira",),
    "lixo": ("coleta", "reciclagem"),
    "obra": ("obras", "reformas"),
    "obras": ("reformas",),
    "reforma": ("obras", "reformas"),
    "reformar": ("obras", "reformas"),
    "pintar": ("obras", "reformas"),
    "pintura": ("obras", "reformas"),
    "mudanca": ("mudancas",),
    "elevador": ("elevadores",),
    "visitante": ("visitantes", "portaria"),
}


def _tokens(text: str) -> set[str]:
    """Tokens normalizados (sem acento, minúsculos) para comparar consulta e capítulo."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)
    )
    words = re.findall(r"[a-z0-9]+", sem_acento.lower())
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def _expand(tokens: set[str]) -> set[str]:
    """Aplica os sinônimos da fala do morador (não muda o texto devolvido)."""
    out = set(tokens)
    for t in tokens:
        out.update(SYNONYMS.get(t, ()))
    return out


@lru_cache(maxsize=1)
def _chapters() -> list[dict]:
    text = REGULATIONS_MD.read_text(encoding="utf-8")
    lines = text.splitlines()
    chapters: list[dict] = []
    current: dict | None = None
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
    for c in chapters:
        c["tokens"] = _tokens(c["title"] + "\n" + c["body"])
        c["title_tokens"] = _tokens(c["title"])
    return chapters


@lru_cache(maxsize=1)
def _idf() -> dict[str, float]:
    """IDF de cada token entre os capítulos (token raro = peso maior)."""
    chapters = _chapters()
    total = len(chapters) or 1
    freq: Counter[str] = Counter()
    for c in chapters:
        freq.update(c["tokens"])
    return {t: math.log((total + 1) / (d + 1)) + 1.0 for t, d in freq.items()}


def _score(query_tokens: set[str], chapter: dict, idf: dict[str, float]) -> float:
    titulo = sum(idf.get(t, 1.0) for t in (query_tokens & chapter["title_tokens"]))
    corpo = sum(idf.get(t, 1.0) for t in (query_tokens & chapter["tokens"]))
    return 2.0 * titulo + corpo


def search(query: str, max_chapters: int = 2, max_chars: int = 1200,
           min_ratio: float = 0.75) -> str:
    """Retorna os capítulos mais relevantes para a consulta (formato curto).

    - Nenhuma coincidência -> mensagem de aviso (não inventar regras).
    - Coincidências -> no máximo `max_chapters` capítulos, recortados, e apenas
      os que ficam a `min_ratio` do melhor score.
    """
    q = _expand(_tokens(query))
    chapters = _chapters()
    if not chapters:
        return "Não consegui localizar essa informação no regulamento."
    idf = _idf()
    ranked = sorted(
        ((_score(q, c, idf), c) for c in chapters), key=lambda x: -x[0]
    )
    melhor = ranked[0][0]
    if melhor <= 0:
        return ("Não há nenhuma regra específica sobre isso no regulamento. "
                "Como regra geral aplica o Capítulo I (Disposições gerais).")
    limite = melhor * min_ratio
    best = [c for score, c in ranked if score >= limite][:max_chapters]
    excerpts = []
    for c in best:
        body = c["body"].strip()
        excerpts.append(f"### {c['title']}\n{body[:max_chars]}")
    return "\n\n".join(excerpts[:max_chapters])
