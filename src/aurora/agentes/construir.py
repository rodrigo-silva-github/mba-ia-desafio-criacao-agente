"""Construção de agentes, App e Runner (sessões persistidas em SQLite).

Seleção do modelo:
- Com `GEMINI_API_KEY` configurada -> modelos Gemini reais (por agente).
- Sem chave -> modelo fake determinista (registrado no registry do ADK),
  para que a API possa ser testada offline de ponta a ponta.

Topologia (decisão D5, spike 001): especialistas com `mode='single_turn'`,
expostos como tools do principal. Sem `transfer_to_agent`.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import LLMRegistry
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from .. import config
from . import tools as herramientas
from .fake_llm import FakeLlm

APP_NAME = "aurora"
FAKE = {"main": "aurora-fake-main", "reservas": "aurora-fake-reservas",
        "visitantes": "aurora-fake-visitantes", "regulamento": "aurora-fake-regulamento"}

INSTRUCCION_PRINCIPAL = (
    "Você é o assistente virtual do Residencial Aurora, um condomínio. "
    "O morador (apartamento definido na sessão) faz pedidos em texto.\n"
    "Regras:\n"
    "- Reservas, cancelamento, disponibilidade ou listado de reservas -> chame "
    "especialista_reservas(request=pedido).\n"
    "- Visitantes -> especialista_visitantes(request=pedido).\n"
    "- Regras do regulamento interno -> especialista_regulamento(request=consulta).\n"
    "- Não respondas resultados que não venham de uma tool; usa apenas o que "
    "elas devolvam. Nunca inventes códigos de reserva, nomes ou datos.\n"
    "- Quando uma tool exija confirmação, o sistema pedirá a aprovação do "
    "morador automáticamente; espere até que essa confirmação seja resolvida."
)

INSTRUCCION_RESERVAS = (
    "Você é o especialista de reservas de áreas comuns do Aurora.\n"
    "- reservar_area(area, data): novas reservas (exige confirmação do morador).\n"
    "- cancelar_reserva(codigo): cancelamentos (também exige confirmação).\n"
    "- esta_libre(area, data): para consultar disponibilidade.\n"
    "- listar_reservas(): para ver as do apartamento da sessão.\n"
    "O apartamento da sessão é fixo (não o pidas ao usuário); a tool já o "
    "injeta. Confirma com o morador o código de reserva antes de cancelar."
)

INSTRUCCION_VISITANTES = (
    "Você é o especialista de visitantes do Aurora.\n"
    "- autorizar_visita(nome, data): para registrar a entrada de um visitante.\n"
    "- listar_visitantes(): para ver os autorizados do apartamento da sessão.\n"
    "O apartamento vem da sessão; não o pidas ao usuário."
)

INSTRUCCION_REGULAMENTO = (
    "Você é o especialista de regras do condomínio Aurora.\n"
    "- consultar_regulamento(consulta): busca no regulamento interno os "
    "capítulos mais relevantes para a consulta.\n"
    "- Não inventes regras; devolve só o que devolva a tool.\n"
    "- Se a consulta não é de regras, devolve o pedido do morador tal qual "
    "(o principal o reenvia ao especialista correcto)."
)


def _modelos() -> dict[str, str]:
  if config.GEMINI_API_KEY:
    from google.adk.models.gemini_llm import get_gemini_models

    disponibles = get_gemini_models()
    mapeo = {"main": config.MODELO_PRINCIPAL, "reservas": config.MODELO_RESERVAS,
             "visitantes": config.MODELO_VISITANTES, "regulamento": config.MODELO_REGULAMENTO}
    for clave, nome in mapeo.items():
      if nome not in disponibles:
        mapeo[clave] = next(
            (n for n in disponibles if "flash" in n), next(iter(disponibles), "gemini-2.5-flash")
        )
    return mapeo
  LLMRegistry.register(FakeLlm)
  return dict(FAKE)


def construir_agentes(modelos: dict[str, str]) -> dict[str, Agent]:
  """Agente principal + 3 especialistas (single_turn)."""
  especialista_reservas = Agent(
      name="especialista_reservas",
      model=modelos["reservas"],
      instruction=INSTRUCCION_RESERVAS,
      mode="single_turn",
      tools=herramientas.TOOLS_RESERVAS,
  )
  especialista_visitantes = Agent(
      name="especialista_visitantes",
      model=modelos["visitantes"],
      instruction=INSTRUCCION_VISITANTES,
      mode="single_turn",
      tools=herramientas.TOOLS_VISITANTES,
  )
  especialista_regulamento = Agent(
      name="especialista_regulamento",
      model=modelos["regulamento"],
      instruction=INSTRUCCION_REGULAMENTO,
      mode="single_turn",
      tools=herramientas.TOOLS_REGULAMENTO,
  )
  principal = Agent(
      name="agente_principal",
      model=modelos["main"],
      instruction=INSTRUCCION_PRINCIPAL,
      sub_agents=[especialista_reservas, especialista_visitantes, especialista_regulamento],
  )
  return {"principal": principal, "reservas": especialista_reservas,
          "visitantes": especialista_visitantes, "regulamento": especialista_regulamento}


def construir_app() -> App:
  """App com sessão persistida (SQLite) e topologia single_turn."""
  agentes = construir_agentes(_modelos())
  return App(
      name=APP_NAME,
      root_agent=agentes["principal"],
      resumability_config=ResumabilityConfig(is_resumable=True),
  )


def novo_runner(app: App | None = None) -> Runner:
  """Runner com DatabaseSessionService sobre o banco de sessões."""
  service = DatabaseSessionService(db_url=config.URL_SESSOES)
  return Runner(app=app or construir_app(), session_service=service, auto_create_session=True)


async def criar_sessao(runner: Runner, user_id: str, apartamento: str) -> str:
  """Crea a sessão do morador com o apartamento fixo no estado."""
  session = await runner.session_service.create_session(
      app_name=APP_NAME, user_id=user_id, state={"apartamento": apartamento}
  )
  return session.id