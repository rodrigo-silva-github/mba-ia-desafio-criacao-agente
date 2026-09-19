"""Configuração do projeto (variáveis de ambiente + dados de seed)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Carga do .env, se existir (nunca versionado; .env.example traz os nomes).
load_dotenv()

RAIZ = Path(__file__).resolve().parent.parent.parent
DATOS = RAIZ / "dados"

# Arquivos de seed (só leitura; o avaliador confirma que ficam idénticos).
APARTAMENTOS_JSON = DATOS / "apartamentos.json"
AREAS_JSON = DATOS / "areas.json"
RESERVAS_JSON = DATOS / "reservas.json"
VISITANTES_JSON = DATOS / "visitantes.json"
REGULAMENTO_MD = DATOS / "regulamento.md"

# Caminhos dos bancos em runtime (fora do Git; ver .gitignore -> var/).
def _caminho_var(nome: str, padrao: str) -> str:
  valor = os.getenv(nome) or padrao
  if not os.path.isabs(valor):
    valor = str((RAIZ / valor).resolve())
  os.makedirs(os.path.dirname(valor), exist_ok=True)
  return valor

BANCO_SESSOES = _caminho_var("CAMINHO_BANCO_SESSOES", "var/aurora_sessoes.db")
BANCO_DADOS = _caminho_var("CAMINHO_BANCO_DADOS", "var/aurora_dados.db")
URL_SESSOES = f"sqlite+aiosqlite:///{BANCO_SESSOES}"

# Chaves e modelos (nomes reservados; usados nas Fases 3+).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODELO_PRINCIPAL = os.getenv("MODELO_PRINCIPAL", "gemini-2.5-flash")
MODELO_RESERVAS = os.getenv("MODELO_RESERVAS", "gemini-2.5-flash")
MODELO_VISITANTES = os.getenv("MODELO_VISITANTES", "gemini-2.5-flash")
MODELO_REGULAMENTO = os.getenv("MODELO_REGULAMENTO", "gemini-2.5-flash")


def cargar_area_ids() -> set[str]:
  """IDs de áreas conhecidas (para validar entradas)."""
  import json

  with open(AREAS_JSON, encoding="utf-8") as f:
    areas = json.load(f)
  return {a["id"] for a in areas}


def cargar_apartamentos() -> set[str]:
  """Números de apartamentos conhecidos."""
  import json

  with open(APARTAMENTOS_JSON, encoding="utf-8") as f:
    aptos = json.load(f)
  return {a["numero"] for a in aptos}