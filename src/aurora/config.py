"""Configuração do projeto: variáveis de ambiente e rotas de seed.

Carrega-se o `.env` se existir (nunca versionado; o `.env.example` versionado
traz os nomes das variáveis, com a chave vazia e os defaults não secretos de
modelo e caminhos). Os arquivos de `dados/` são só leitura:
o estado vivo vive nos bancos SQLite de `var/` (fora do Git).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "dados"

# Arquivos de seed (só leitura; o avaliador confirma que ficam idênticos).
APARTMENTS_JSON = DATA_DIR / "apartamentos.json"
AREAS_JSON = DATA_DIR / "areas.json"
RESERVATIONS_JSON = DATA_DIR / "reservas.json"
VISITORS_JSON = DATA_DIR / "visitantes.json"
REGULATIONS_MD = DATA_DIR / "regulamento.md"


def _runtime_path(env_name: str, default: str) -> str:
    """Resolve um caminho de runtime a partir do ambiente (relativa -> absoluta)."""
    value = os.getenv(env_name) or default
    if not os.path.isabs(value):
        value = str((ROOT / value).resolve())
    os.makedirs(os.path.dirname(value), exist_ok=True)
    return value


# Bancos de runtime: sessões do ADK e dados de negócio, em arquivos separados
# (decisão D3a do planejamento).
SESSION_DB_PATH = _runtime_path("SESSIONS_DB_PATH", "var/aurora_sessoes.db")
SESSION_DB_URL = f"sqlite+aiosqlite:///{SESSION_DB_PATH}"
BUSINESS_DB_PATH = _runtime_path("BUSINESS_DB_PATH", "var/aurora_dados.db")

# Chave e modelos Gemini (por agente; sem chave, cai no modelo fake).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MAIN_MODEL = os.getenv("MAIN_MODEL", "gemini-2.5-flash")
RESERVATIONS_MODEL = os.getenv("RESERVATIONS_MODEL", "gemini-2.5-flash")
VISITORS_MODEL = os.getenv("VISITORS_MODEL", "gemini-2.5-flash")
REGULATIONS_MODEL = os.getenv("REGULATIONS_MODEL", "gemini-2.5-flash")


def load_areas() -> dict[str, float]:
    """Áreas conhecidas: id -> taxa (0 = sem taxa, não gera cobrança)."""
    import json

    with open(AREAS_JSON, encoding="utf-8") as f:
        areas = json.load(f)
    return {a["id"]: float(a["taxa"]) for a in areas}


def load_area_names() -> dict[str, str]:
    """Nome de exibição de cada área (id -> nome).

    Usado para normalizar o que o morador (e o modelo) escrevem: o morador
    pede "o salão de festas", o id é `salao-de-festas`.
    """
    import json

    with open(AREAS_JSON, encoding="utf-8") as f:
        areas = json.load(f)
    return {a["id"]: a["nome"] for a in areas}


def load_apartments() -> set[str]:
    """Números de apartamentos conhecidos (para validar entradas)."""
    import json

    with open(APARTMENTS_JSON, encoding="utf-8") as f:
        entries = json.load(f)
    return {a["numero"] for a in entries}