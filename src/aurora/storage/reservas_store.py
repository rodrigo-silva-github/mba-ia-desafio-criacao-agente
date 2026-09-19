"""Store de reservas sobre SQLite.

Contratos:
- reservar() grava a reserva; a exclusividade área+data vale NO INSTANTE
  do INSERT (UNIQUE(area, data)) -> devolve data_ocupada se o slot já foi
  ocupado, sem que o modelo precise "reconferir" nada.
- O código (RSV-####) é único entre TODAS as reservas, incluídas as
  canceladas (a fila fica com ativo=0; não se faz DELETE).
- cancelar() só toca reservas do próprio apartamento (Garantia 2).
- Ninguna função recebe o apartamento como argumento do modelo: a sessão
  fornece sempre (se garante na Fase 3/4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import aiosqlite

from . import db

_MAX_REINTENTOS_CODIGO = 5
_RE_CODIGO = re.compile(r"^RSV-(\d+)$")


@dataclass
class Reserva:
  codigo: str
  area: str
  data: str


@dataclass
class ResultadoReserva:
  ok: bool
  reserva: Reserva | None = None
  motivo: str | None = None


class SlotOcupado(Exception):
  """A área já tem reserva ativa nessa data (Garantia 5)."""


async def _proximo_codigo(conn: aiosqlite.Connection) -> str:
  """Maior código numérico existente (incl. cancelados) + 1 -> 'RSV-####'."""
  async with conn.execute("SELECT codigo FROM reservas") as cur:
    filas = await cur.fetchall()
  max_num = 0
  for (codigo,) in filas:
    m = _RE_CODIGO.match(codigo or "")
    if m:
      max_num = max(max_num, int(m.group(1)))
  return f"RSV-{max_num + 1:04d}"


async def _insertar_con_codigo(
    conn: aiosqlite.Connection,
    apartamento: str,
    area: str,
    data: str,
) -> str:
  """INSERT com reintento se o código colisiona (raza entre 2 escrituras)."""
  for _ in range(_MAX_REINTENTOS_CODIGO):
    codigo = await _proximo_codigo(conn)
    try:
      await conn.execute(
          "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
          (codigo, apartamento, area, data),
      )
      return codigo
    except aiosqlite.IntegrityError:
      async with conn.execute(
          "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativo = 1",
          (area, data),
      ) as cur:
        ocupado = await cur.fetchone()
      if ocupado:
        raise SlotOcupado(f"Área {area} já reservada para {data}") from None
      continue  # colisión de código: tenta novamente com max recalculado
  codigo = await _proximo_codigo(conn)
  await conn.execute(
      "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
      (codigo, apartamento, area, data),
  )
  return codigo


async def reservar(apartamento: str, area: str, data: str) -> ResultadoReserva:
  """Cria a reserva numa transação única. Nunca duas ativas por área+data."""
  conn = await db.abrir()
  try:
    await conn.execute("BEGIN IMMEDIATE")
    codigo = await _insertar_con_codigo(conn, apartamento, area, data)
    await conn.commit()
    return ResultadoReserva(ok=True, reserva=Reserva(codigo=codigo, area=area, data=data))
  except SlotOcupado:
    await conn.rollback()
    return ResultadoReserva(ok=False, motivo="data_ocupada")
  except aiosqlite.IntegrityError:
    await conn.rollback()
    return ResultadoReserva(ok=False, motivo="data_ocupada")
  finally:
    await conn.close()


async def cancelar(apartamento: str, codigo: str) -> bool:
  """Cancela UNA reserva (ativa) do próprio apartamento."""
  conn = await db.abrir()
  try:
    await conn.execute("BEGIN IMMEDIATE")
    cur = await conn.execute(
        "UPDATE reservas SET ativo = 0 WHERE codigo = ? AND apartamento = ? AND ativo = 1",
        (codigo, apartamento),
    )
    await conn.commit()
    return cur.rowcount > 0
  finally:
    await conn.close()


async def listar_apartamento(apartamento: str) -> list[dict[str, str]]:
  """Reservas activas do apartamento (verificação e tools)."""
  conn = await db.abrir()
  try:
    async with conn.execute(
        "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND ativo = 1 ORDER BY data, area",
        (apartamento,),
    ) as cur:
      filas = await cur.fetchall()
    return [{"codigo": c, "area": a, "data": d} for c, a, d in filas]
  finally:
    await conn.close()


async def esta_libre(area: str, data: str) -> bool:
  """Agenda: devolve SÓ livre/ocupado; nunca de quem é a reserva (G2)."""
  conn = await db.abrir()
  try:
    async with conn.execute(
        "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativo = 1",
        (area, data),
    ) as cur:
      return await cur.fetchone() is None
  finally:
    await conn.close()


async def todas_ativas() -> list[dict[str, str]]:
  """Todas as reservas ativas (backup/verificação global)."""
  conn = await db.abrir()
  try:
    async with conn.execute(
        "SELECT codigo, apartamento, area, data FROM reservas WHERE ativo = 1 ORDER BY data",
    ) as cur:
      filas = await cur.fetchall()
    return [{"codigo": c, "apartamento": a, "area": x, "data": d} for c, a, x, d in filas]
  finally:
    await conn.close()