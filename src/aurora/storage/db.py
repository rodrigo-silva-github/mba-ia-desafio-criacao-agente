"""Conexão SQLite (async) e schema dos dados de negócio.

Regras de ouro:
- Os arquivos de dados/ são SEED (só leitura): o estado vivo vive em
  var/aurora_dados.db, fora do Git.
- A exclusividade da reserva (Garantia 5) é posta pelo SQLite no instante
  do INSERT: UNIQUE(area, data) entre reservas ativas.
- Os códigos de reserva nunca são reutilizados, nem os de canceladas: a tabela
  conserva as linhas canceladas (flag ativo=0) e o código novo é calculado
  sobre TODOS os códigos existentes (max+1, com reintento em caso de colisão).
- Tabelas `sessions` e `confirmations`: mapeamento session_id -> user_id mais
  o estado das pendências da API (Garantia 1 e reinício, Fase 4).
"""

from __future__ import annotations

import aiosqlite

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservas (
    codigo      TEXT PRIMARY KEY,
    apartamento TEXT NOT NULL,
    area        TEXT NOT NULL,
    data        TEXT NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1
);

-- Exclusividade SOMENTE entre reservas ativas (Garantia 5, no instante do
-- INSERT). As canceladas conservam sua linha (códigos não reutilizáveis) e
-- não bloqueiam re-reservar o mesmo slot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservas_area_data_ativa
    ON reservas (area, data) WHERE ativo = 1;

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);

-- Mapeamento da API: session_id do ADK -> user_id + apartamento fixo
-- (Garantia 2). Permite encontrar user_id após um restart (Garantia 3).
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    apartment  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Pendências de confirmação servidas pela API (Garantia 1 + D9c). O status
-- passa de 'pending' a 'answered' de forma atómica (UPDATE condicional) para
-- que duas respostas simultâneas ao mesmo id nunca executem duas vezes.
CREATE TABLE IF NOT EXISTS confirmations (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    details     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    confirmed   INTEGER,
    created_at  TEXT NOT NULL,
    answered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_confirmations_session ON confirmations(session_id);
"""


async def connect() -> aiosqlite.Connection:
    """Abre uma conexão nova (WAL + busy_timeout). Feche com close()."""
    conn = await aiosqlite.connect(config.BUSINESS_DB_PATH, timeout=5.0)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db() -> None:
    """Cria o schema se não existe. Idempotente."""
    conn = await connect()
    try:
        await conn.execute("BEGIN")
        await conn.executescript(SCHEMA)
        await conn.commit()
    finally:
        await conn.close()