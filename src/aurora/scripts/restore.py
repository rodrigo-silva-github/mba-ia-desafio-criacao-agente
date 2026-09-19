"""Restore dos dados iniciais do condomínio.

Uso:  uv run python -m aurora.scripts.restore

1. Recarrega reservas e visitantes a partir dos arquivos de dados/ (seed,
   que NUNCA são alterados).
2. Apaga as sessões do assistente, o mapeamento sessions<->user e as
   confirmações pendentes (decisão documentada: o comando deixa o sistema
   no estado inicial completo; comportamento livre pelo enunciado).
3. Nunca escreve nos arquivos de dados/ (apartamentos/áreas são só leitura
   neste projeto).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from .. import config
from ..storage import confirmations_store, db, sessions_store

SCRIPT = Path(__file__).resolve()


def _read_json(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def restore() -> None:
    await db.init_db()
    await confirmations_store.delete_all()
    await sessions_store.delete_all()

    reservations_seed = _read_json(config.RESERVATIONS_JSON)
    visitors_seed = _read_json(config.VISITORS_JSON)

    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("DELETE FROM visitantes")
        await conn.execute("DELETE FROM reservas")
        for r in reservations_seed:
            await conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (r["codigo"], r["apartamento"], r["area"], r["data"]),
            )
        for v in visitors_seed:
            await conn.execute(
                "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
                (v["apartamento"], v["nome"], v["data"]),
            )
        await conn.commit()
    finally:
        await conn.close()

    # Sessões do ADK: apaga o banco (estado inicial completo).
    if os.path.exists(config.SESSION_DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            path = config.SESSION_DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)

    print(
        f"Restaurado: {len(reservations_seed)} reservas, {len(visitors_seed)} "
        f"visitantes; sessões apagadas ({config.SESSION_DB_PATH})."
    )


if __name__ == "__main__":
    asyncio.run(restore())
    sys.exit(0)