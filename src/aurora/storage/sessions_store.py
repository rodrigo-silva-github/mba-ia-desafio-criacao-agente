"""Mapeamento de sessões da API (session_id ADK <-> user_id).

A API expõe sessões só por `session_id`; o ADK precisa `user_id` para
recuperar/continuar uma sessão após um restart (Garantia 3). Esta tabela
guarda esse mapeamento com o apartamento fixo (Garantia 2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def add(session_id: str, user_id: str, apartment: str) -> None:
    """Registra uma sessão nova da API."""
    conn = await db.connect()
    try:
        await conn.execute(
            "INSERT INTO sessions (session_id, user_id, apartment, created_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, apartment, _now()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get(session_id: str) -> dict | None:
    """Dados da sessão (user_id, apartment) ou None se não existe."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT session_id, user_id, apartment FROM sessions WHERE session_id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        sid, user_id, apartment = row
        return {"session_id": sid, "user_id": user_id, "apartment": apartment}
    finally:
        await conn.close()


async def delete_all() -> None:
    """Limpa o mapeamento (restore: estado inicial completo)."""
    conn = await db.connect()
    try:
        await conn.execute("DELETE FROM sessions")
        await conn.commit()
    finally:
        await conn.close()