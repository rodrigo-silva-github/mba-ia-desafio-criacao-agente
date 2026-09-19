"""Conexão SQLite (async) e schema dos dados de negócio.

Regras de ouro:
- Os arquivos de dados/ são SEED (só leitura): o estado vivo vive em
  var/aurora_dados.db, fora do Git.
- A exclusividade da reserva (Garantia 5) é posta pelo SQLite no instante
  do INSERT: UNIQUE(area, data).
- Os códigos de reserva nunca são reutilizados, nem os de canceladas: a tabela
  conserva as filas canceladas (flag ativo=0) e o código novo é calculado
  sobre TODOS os códigos existentes (max+1, com reintento ante colisión).
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

-- Exclusividad SOLO entre reservas activas (Garantia 5, en el instante del
-- INSERT). Las canceladas conservan su fila (códigos no reutilizables) y no
-- bloquean re-reservar el mismo slot.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservas_area_data_ativa
    ON reservas (area, data) WHERE ativo = 1;

CREATE TABLE IF NOT EXISTS visitantes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    apartamento TEXT NOT NULL,
    nome        TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


async def abrir() -> aiosqlite.Connection:
  """Abre uma conexão nova (WAL + busy_timeout). Fechar com close()."""
  conn = await aiosqlite.connect(config.BANCO_DADOS, timeout=5.0)
  await conn.execute("PRAGMA journal_mode=WAL")
  await conn.execute("PRAGMA busy_timeout=5000")
  await conn.execute("PRAGMA foreign_keys=ON")
  return conn


async def iniciar() -> None:
  """Cria o schema se não existe. Idempotente."""
  conn = await abrir()
  try:
    await conn.execute("BEGIN")
    await conn.executescript(SCHEMA)
    await conn.commit()
  finally:
    await conn.close()