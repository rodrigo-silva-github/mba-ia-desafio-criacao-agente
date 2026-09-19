"""Store de reservas sobre SQLite.

Contratos:
- `book()` grava a reserva; a exclusividade área+data vale NO INSTANTE
  do INSERT (UNIQUE(area, data) WHERE ativo=1) -> retorna `date_taken` se
  o slot já está ocupado, sem que o modelo precise "reconferir" nada
  (Garantia 5).
- O código (RSV-####) é único entre TODAS as reservas, incluídas as
  canceladas (a linha fica com ativo=0; não se faz DELETE).
- `cancel()` só toca reservas do próprio apartamento (Garantia 2).
- Nenhuma função recebe o apartamento como argumento do modelo: a sessão o
  fornece sempre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import aiosqlite

from . import db

_MAX_CODE_RETRIES = 5
_CODE_RE = re.compile(r"^RSV-(\d+)$")


@dataclass
class Reservation:
    codigo: str
    area: str
    data: str


@dataclass
class ReservationResult:
    ok: bool
    reservation: Reservation | None = None
    reason: str | None = None


class SlotTaken(Exception):
    """A área já tem reserva ativa nessa data (Garantia 5)."""


async def _next_code(conn: aiosqlite.Connection) -> str:
    """Maior código numérico existente (incl. cancelados) + 1 -> 'RSV-####'."""
    async with conn.execute("SELECT codigo FROM reservas") as cur:
        rows = await cur.fetchall()
    max_num = 0
    for (codigo,) in rows:
        m = _CODE_RE.match(codigo or "")
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"RSV-{max_num + 1:04d}"


async def _insert_with_code(
    conn: aiosqlite.Connection,
    apartment: str,
    area: str,
    data: str,
) -> str:
    """INSERT com reintento se o código colide (corrida entre 2 escrituras)."""
    for _ in range(_MAX_CODE_RETRIES):
        codigo = await _next_code(conn)
        try:
            await conn.execute(
                "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
                (codigo, apartment, area, data),
            )
            return codigo
        except aiosqlite.IntegrityError:
            async with conn.execute(
                "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativo = 1",
                (area, data),
            ) as cur:
                occupied = await cur.fetchone()
            if occupied:
                raise SlotTaken(f"Área {area} já reservada para {data}") from None
            continue  # colisão de código: reintenta com max recalculado
    codigo = await _next_code(conn)
    await conn.execute(
        "INSERT INTO reservas (codigo, apartamento, area, data) VALUES (?, ?, ?, ?)",
        (codigo, apartment, area, data),
    )
    return codigo


async def book(apartment: str, area: str, data: str) -> ReservationResult:
    """Cria a reserva em uma transação única. Nunca duas ativas por área+data."""
    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        codigo = await _insert_with_code(conn, apartment, area, data)
        await conn.commit()
        return ReservationResult(ok=True, reservation=Reservation(codigo=codigo, area=area, data=data))
    except SlotTaken:
        await conn.rollback()
        return ReservationResult(ok=False, reason="date_taken")
    except aiosqlite.IntegrityError:
        await conn.rollback()
        return ReservationResult(ok=False, reason="date_taken")
    finally:
        await conn.close()


async def cancel(apartment: str, codigo: str) -> bool:
    """Cancela UMA reserva (ativa) do próprio apartamento."""
    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            "UPDATE reservas SET ativo = 0 WHERE codigo = ? AND apartamento = ? AND ativo = 1",
            (codigo, apartment),
        )
        await conn.commit()
        return cur.rowcount > 0
    finally:
        await conn.close()


async def list_apartment(apartment: str) -> list[dict[str, str]]:
    """Reservas ativas do apartamento (verificação e tools)."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT codigo, area, data FROM reservas WHERE apartamento = ? AND ativo = 1 ORDER BY data, area",
            (apartment,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"codigo": c, "area": a, "data": d} for c, a, d in rows]
    finally:
        await conn.close()


async def is_available(area: str, data: str) -> bool:
    """Agenda: retorna SOMENTE livre/ocupada; nunca de quem é a reserva (G2)."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT 1 FROM reservas WHERE area = ? AND data = ? AND ativo = 1",
            (area, data),
        ) as cur:
            return await cur.fetchone() is None
    finally:
        await conn.close()


async def all_active() -> list[dict[str, str]]:
    """Todas as reservas ativas (backup/verificação global)."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT codigo, apartamento, area, data FROM reservas WHERE ativo = 1 ORDER BY data",
        ) as cur:
            rows = await cur.fetchall()
        return [{"codigo": c, "apartamento": a, "area": x, "data": d} for c, a, x, d in rows]
    finally:
        await conn.close()