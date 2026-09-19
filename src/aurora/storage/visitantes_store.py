"""Store de autorizações de visita sobre SQLite."""

from __future__ import annotations

from . import db


async def autorizar(apartamento: str, nome: str, data: str) -> dict[str, str]:
  """Registra uma autorização de visita (só acrescenta, nunca reescreve)."""
  conn = await db.abrir()
  try:
    await conn.execute("BEGIN IMMEDIATE")
    cur = await conn.execute(
        "INSERT INTO visitantes (apartamento, nome, data) VALUES (?, ?, ?)",
        (apartamento, nome, data),
    )
    await conn.commit()
    return {"id": cur.lastrowid, "nome": nome, "data": data}
  finally:
    await conn.close()


async def listar_apartamento(apartamento: str) -> list[dict[str, str]]:
  """Autorizações do apartamento (para verificação e tools)."""
  conn = await db.abrir()
  try:
    async with conn.execute(
        "SELECT nome, data FROM visitantes WHERE apartamento = ? ORDER BY data, nome",
        (apartamento,),
    ) as cur:
      filas = await cur.fetchall()
    return [{"nome": n, "data": d} for n, d in filas]
  finally:
    await conn.close()