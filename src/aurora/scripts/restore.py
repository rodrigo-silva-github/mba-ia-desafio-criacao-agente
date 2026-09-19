"""Restaura dados iniciais do condomínio.

Uso:  uv run python -m aurora.scripts.restore

1. Recarrega reservas e visitantes a partir dos arquivos de dados/ (seed,
   que NUNCA são alterados).
2. Apaga as sessões do assistente (decisão documentada: o comando deixa o
   sistema no estado inicial completo; comportamento livre pelo enunciado).
3. Nunca escreve nos arquivos de dados/ (apartamentos/áreas são só leitura
   neste projeto).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .. import config
from ..storage import db

SCRIPT = Path(__file__).resolve()


def _ler_json(caminho: Path) -> list[dict]:
  with open(caminho, encoding="utf-8") as f:
    return json.load(f)


async def restaurar() -> None:
  await db.iniciar()
  reservas_seed = _ler_json(config.RESERVAS_JSON)
  visitantes_seed = _ler_json(config.VISITANTES_JSON)

  conn = await db.abrir()
  try:
    await conn.execute("BEGIN IMMEDIATE")
    await conn.execute("DELETE FROM visitantes")
    await conn.execute("DELETE FROM reservas")
    for r in reservas_seed:
      await conn.execute(
          "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
          (r["codigo"], r["apartamento"], r["area"], r["data"]),
      )
    for v in visitantes_seed:
      await conn.execute(
          "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
          (v["apartamento"], v["nome"], v["data"]),
      )
    await conn.commit()
  finally:
    await conn.close()

  # Sessões: apaga o banco de sessões do ADK (estado inicial completo).
  if os.path.exists(config.BANCO_SESSOES):
    for sufixo in ("", "-wal", "-shm"):
      caminho = config.BANCO_SESSOES + sufixo
      if os.path.exists(caminho):
        os.remove(caminho)

  print(
      f"Restaurado: {len(reservas_seed)} reservas, {len(visitantes_seed)} "
      f"visitantes; sessões apagadas ({config.BANCO_SESSOES})."
  )


if __name__ == "__main__":
  import asyncio

  asyncio.run(restaurar())
  sys.exit(0)