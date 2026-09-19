"""Store de autorizações de visita sobre SQLite."""

from __future__ import annotations

from . import db


async def authorize(apartment: str, name: str, data: str) -> dict[str, str]:
    """Registra uma autorização de visita (só adiciona, nunca reescreve)."""
    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
            (apartment, name, data),
        )
        await conn.commit()
        return {"id": cur.lastrowid, "nome": name, "data": data}
    finally:
        await conn.close()


async def list_apartment(apartment: str) -> list[dict[str, str]]:
    """Autorizações do apartamento (para verificação e tools)."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, nome",
            (apartment,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"nome": n, "data": d} for n, d in rows]
    finally:
        await conn.close()