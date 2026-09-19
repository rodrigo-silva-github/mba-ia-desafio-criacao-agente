"""Store de pendências de confirmação da API (Garantia 1, D9c).

A tabela é a fonte de verdade das `confirmacoes_pendentes` devolvidas pela
API e o cadeado de idempotência:

- `add_or_ignore`: guarda uma pendência detectada em um turno (id = id do FC
  sintético `adk_request_confirmation` que o cliente deve responder).
- `claim`: UPDATE condicional `pending -> answered`; retorna True só se a
  linha seguia pendente. Duas respostas simultâneas ao mesmo id: a primeira
  vence, a segunda recebe False -> 409 sem executar nada (D9c, passo 8).
- O reenvio sequencial do mesmo id também recebe 409 (status != pending).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def add_or_ignore(session_id: str, confirmation_id: str, action: str,
                        details: dict) -> None:
    """Persiste uma pendência nova (INSERT IGNORE: id repetido não sobrescreve)."""
    conn = await db.connect()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO confirmations "
            "(id, session_id, action, details, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (confirmation_id, session_id, action, _json(details), _now()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get(session_id: str, confirmation_id: str) -> dict | None:
    """Linha da pendência (com detalhes parseados) ou None."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT id, session_id, action, details, status, confirmed "
            "FROM confirmations WHERE session_id = ? AND id = ?",
            (session_id, confirmation_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        cid, sid, action, details_raw, status, confirmed = row
        return {"id": cid, "session_id": sid, "action": action,
                "details": _unjson(details_raw), "status": status,
                "confirmed": confirmed}
    finally:
        await conn.close()


async def list_pending(session_id: str) -> list[dict]:
    """Todas as pendências ativas da sessão (para a resposta da API)."""
    conn = await db.connect()
    try:
        async with conn.execute(
            "SELECT id, action, details FROM confirmations "
            "WHERE session_id = ? AND status = 'pending' ORDER BY created_at, id",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"id": cid, "action": action, "details": _unjson(details_raw)}
                for cid, action, details_raw in rows]
    finally:
        await conn.close()


async def claim(session_id: str, confirmation_id: str, confirmed: bool) -> bool:
    """Transição atômica pending -> answered. Retorna True só se a linha ainda estava pendente."""
    conn = await db.connect()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute(
            "UPDATE confirmations SET status = 'answered', confirmed = ?, answered_at = ? "
            "WHERE session_id = ? AND id = ? AND status = 'pending'",
            (int(confirmed), _now(), session_id, confirmation_id),
        )
        await conn.commit()
        return cur.rowcount > 0
    finally:
        await conn.close()


async def delete_all() -> None:
    """Limpa as pendências (restore: estado inicial completo)."""
    conn = await db.connect()
    try:
        await conn.execute("DELETE FROM confirmations")
        await conn.commit()
    finally:
        await conn.close()


def _json(details: dict) -> str:
    import json

    return json.dumps(details, ensure_ascii=False, sort_keys=True)


def _unjson(raw: str) -> dict:
    import json

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}