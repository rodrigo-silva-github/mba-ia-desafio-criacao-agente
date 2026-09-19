"""Índice do regulamento interno por capítulos + busca por termos.

Sem dependências externas: os capítulos são extraídos do markdown uma única
vez e pontuados por sobreposição de tokens com a consulta.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ..config import REGULAMENTO_MD

TOKENS_IRRELEVANTES = {
    "a", "o", "e", "de", "da", "do", "del", "á", "com", "que", "para", "por",
    "se", "en", "no", "na", "los", "las", "el", "al", "um", "uma", "una",
}


def _tokens(texto: str) -> set[str]:
  palavras = re.findall(r"[a-záéíóúñçàèìòù]+", texto.lower())
  return {p for p in palavras if len(p) > 2 and p not in TOKENS_IRRELEVANTES}


@lru_cache(maxsize=1)
def _capitulos() -> list[dict[str, str]]:
  texto = REGULAMENTO_MD.read_text(encoding="utf-8")
  linhas = texto.splitlines()
  caps: list[dict[str, str]] = []
  atual: dict[str, str] | None = None
  for linha in linhas:
    if linha.startswith("## "):
      if atual is not None:
        caps.append(atual)
      atual = {"titulo": linha[3:].strip(), "texto": ""}
      continue
    if atual is not None:
      atual["texto"] += linha + "\n"
  if atual is not None:
    caps.append(atual)
  return [{**c, "tokens": _tokens(c["titulo"] + "\n" + c["texto"])} for c in caps]


def consultar(consulta: str, max_capitulos: int = 2, max_chars: int = 1200) -> str:
  """Devolve os capítulos mais relevantes para a consulta (formato curto).

  - Nenhuna coincidencia -> mensagem de aviso (não inventar regras).
  - Coincidencias -> máximo `max_capitulos` capítulos, recortados.
  """
  q = _tokens(consulta)
  caps = _capitulos()
  if not caps:
    return "Não consegui localizar essa informação no regulamento."
  puntuados = sorted(
      ((len(q & c["tokens"]), c) for c in caps), key=lambda x: -x[0]
  )
  melhores = [c for score, c in puntuados if score > 0][:max_capitulos]
  if not melhores:
    return ("Não há nenhuna regra específica sobre isso no regulamento. "
            "Como regra geral aplica o Capítulo I (Disposições gerais).")
  trechos = []
  for c in melhores:
    corpo = c["texto"].strip()
    trechos.append(f"### {c['titulo']}\n{corpo[:max_chars]}")
  return "\n\n".join(trechos[:max_capitulos])