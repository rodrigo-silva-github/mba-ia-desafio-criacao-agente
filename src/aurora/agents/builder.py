"""Construção de agentes, App e Runner (sessões persistidas em SQLite).

Seleção do modelo:
- Com `GEMINI_API_KEY` configurada -> modelos Gemini reais (por agente).
- Sem chave -> modelo fake determinista (registrado no registry do ADK),
  para que a API possa ser testada offline de ponta a ponta.

Topologia (decisão D5): especialistas com `mode='single_turn'`, expostos
como tools do principal. Sem `transfer_to_agent`.

Re-fixação da raiz (fix do spike 002, ADK 2.9.2): `find_agent_to_run` decide
pelo ÚLTIMO evento da sessão e é chamado ANTES de o FunctionResponse do turno
ser anexado. Os especialistas são transferíveis, então sem intervenção o
router os re-escolhe para as mensagens seguintes e a sessão fica presa no
último domínio usado (S1 mistura reservas, visitante e regulamento). A
solução é anexar um evento sintético `author=main_agent` ao FIM de cada turno
resolvido: com ele por último, o router devolve sempre a raiz no turno
seguinte. `anchor_root()` faz isso, chamada por `api/main.py`.

Regra crítica (a origem da pendência duplicada): NUNCA se ancora um turno que
terminou pedindo confirmação. Nesse caso o último evento tem de continuar
sendo o `adk_request_confirmation` do especialista, porque é ele que faz o
router devolver o especialista no turno de retomada — se a raiz for escolhida,
ela re-executa o pedido original, o especialista reemite a tool e a pendência
duplica (`confirmacoes_pendentes` devolve 2; passos 8/11 falham).
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models import LLMRegistry
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from .. import config
from . import tools
from .fake_llm import FakeLlm

APP_NAME = "aurora"
CONFIRMATION_FC = "adk_request_confirmation"
MAIN_AGENT_NAME = "main_agent"
FAKE_MODELS = {"main": "aurora-fake-main", "reservations": "aurora-fake-reservations",
               "visitors": "aurora-fake-visitors", "regulations": "aurora-fake-regulations"}

MAIN_INSTRUCTION = (
    "Você é o assistente virtual do Residencial Aurora, um condomínio. "
    "O morador (apartamento definido na sessão) faz pedidos em texto.\n"
    "Regras:\n"
    "- Reservas, cancelamento, disponibilidade ou listado de reservas -> chame "
    "reservations_agent(request=pedido).\n"
    "- Visitantes -> visitors_agent(request=pedido).\n"
    "- Dúvidas sobre regras do regulamento interno -> regulations_agent(request=consulta).\n"
    "- Use apenas o que as tools devolvam; nunca invente códigos de reserva, "
    "nomes ou datas.\n"
    "- Quando uma tool exija confirmação, o sistema pedirá a aprovação do "
    "morador automaticamente; espere até que essa confirmação seja resolvida."
)

RESERVATIONS_INSTRUCTION = (
    "Você é o especialista de reservas de áreas comuns do Aurora.\n"
    "- book_area(area, data): novas reservas. O sistema pede confirmação ao "
    "morador quando a área gera cobrança; áreas sem taxa são reservadas direto. "
    "- cancel_reservation(codigo): cancelamentos (não pede confirmação).\n"
    "- check_availability(area, data): disponibilidade (só livre ou ocupada).\n"
    "- list_reservations(): para ver as do apartamento da sessão.\n"
    "O apartamento da sessão é fixo (não o peça ao usuário); a tool já o "
    "injecta."
)

VISITORS_INSTRUCTION = (
    "Você é o especialista de visitantes do Aurora.\n"
    "- authorize_visit(nome, data): para registrar a entrada de um visitante "
    "(o sistema pede confirmação ao morador).\n"
    "- list_visitors(): para ver os autorizados do apartamento da sessão.\n"
    "O apartamento vem da sessão; não o peça ao usuário."
)

REGULATIONS_INSTRUCTION = (
    "Você é o especialista de regras do condomínio Aurora.\n"
    "- query_regulations(consulta): busca no regulamento interno os capítulos "
    "mais relevantes para a consulta.\n"
    "- Não invente regras; retorne só o que a tool retorne.\n"
    "- Se a consulta não é de regras, retorne o pedido do morador tal qual "
    "(o principal o reenvia ao especialista correto)."
)


def _models() -> dict[str, str]:
    """Modelos de cada agente (por agente; sem chave, modelo fake determinista).

    Com `GEMINI_API_KEY`, usa os nomes configurados no `.env` e valida cada um
    com `LLMRegistry.resolve` — o ADK 2.9.2 aceita qualquer nome do padrão
    `gemini-*` (`google_llm.Gemini.supported_models`) e NÃO expõe um helper que
    liste os modelos disponíveis no projeto. A disponibilidade real e a quota
    são conferidas no Google AI Studio e aparecem na primeira chamada ao
    modelo; um nome fora do padrão falha aqui, com mensagem apontando a
    variável do `.env` a corrigir.
    """
    if not config.GEMINI_API_KEY:
        LLMRegistry.register(FakeLlm)
        return dict(FAKE_MODELS)

    mapping = {
        "MAIN_MODEL": config.MAIN_MODEL,
        "RESERVATIONS_MODEL": config.RESERVATIONS_MODEL,
        "VISITORS_MODEL": config.VISITORS_MODEL,
        "REGULATIONS_MODEL": config.REGULATIONS_MODEL,
    }
    for env_name, model_name in mapping.items():
        try:
            LLMRegistry.resolve(model_name)
        except ValueError as exc:  # nome fora de qualquer padrão suportado
            raise ValueError(
                f"Modelo inválido em {env_name}: {model_name!r}. "
                "Ajuste essa variável no .env (veja .env.example)."
            ) from exc
    return {
        "main": config.MAIN_MODEL,
        "reservations": config.RESERVATIONS_MODEL,
        "visitors": config.VISITORS_MODEL,
        "regulations": config.REGULATIONS_MODEL,
    }


def build_agents(models: dict[str, str]) -> dict[str, Agent]:
    """Agente principal + 3 especialistas (single_turn).

    `include_contents="default"` nos especialistas é obrigatório: em agentes
    `single_turn` o ADK assume `include_contents="none"`, e a fatia de
    "turno atual" montada nesse modo descarta o resultado da tool quando o
    turno é uma RETOMADA de confirmação (o `FunctionResponse` do negócio não
    entra no pedido ao modelo). Sem o resultado no contexto o especialista
    reemite a tool, gerando uma SEGUNDA pendência (o bug do passo 11:
    `confirmacoes_pendentes` devolvia 2). Com o histórico completo o
    especialista vê que a tool já respondeu e apenas relata o resultado.

    `disallow_transfer_to_parent=True` é o que torna o roteamento dos turnos
    de texto determinístico: `find_agent_to_run` só mantém um sub-agente no
    comando do próximo turno se ele for "transferível" para a árvore inteira
    (`is_transferable_across_agent_tree` exige
    `disallow_transfer_to_parent is False` no agente e em todos os
    ancestrais). Com a flag, os eventos dos especialistas são ignorados na
    varredura e o router cai no principal — antes disso, a sessão ficava
    presa no especialista do último domínio de forma INTERMITENTE (a
    varredura depende da ordem dos eventos, e os timestamps empatados faziam
    a resposta variar). A retomada de confirmações não depende dessa
    escolha: ela vem do `FunctionResponse` da confirmação pendente
    (`find_matching_function_call`), não da varredura de eventos.
    """
    reservations_agent = Agent(
        name="reservations_agent",
        model=models["reservations"],
        instruction=RESERVATIONS_INSTRUCTION,
        mode="single_turn",
        include_contents="default",
        disallow_transfer_to_parent=True,
        tools=tools.TOOLS_RESERVAS,
    )
    visitors_agent = Agent(
        name="visitors_agent",
        model=models["visitors"],
        instruction=VISITORS_INSTRUCTION,
        mode="single_turn",
        include_contents="default",
        disallow_transfer_to_parent=True,
        tools=tools.TOOLS_VISITANTES,
    )
    regulations_agent = Agent(
        name="regulations_agent",
        model=models["regulations"],
        instruction=REGULATIONS_INSTRUCTION,
        mode="single_turn",
        include_contents="default",
        disallow_transfer_to_parent=True,
        tools=tools.TOOLS_REGULAMENTO,
    )
    main_agent = Agent(
        name=MAIN_AGENT_NAME,
        model=models["main"],
        instruction=MAIN_INSTRUCTION,
        sub_agents=[reservations_agent, visitors_agent, regulations_agent],
    )
    return {"main": main_agent, "reservations": reservations_agent,
            "visitors": visitors_agent, "regulations": regulations_agent}


def build_app() -> App:
    """App com sessão persistida (SQLite) e topologia single_turn."""
    agents = build_agents(_models())
    return App(
        name=APP_NAME,
        root_agent=agents["main"],
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


def new_runner(app: App | None = None) -> Runner:
    """Runner com DatabaseSessionService sobre o banco de sessões."""
    service = DatabaseSessionService(db_url=config.SESSION_DB_URL)
    return Runner(app=app or build_app(), session_service=service, auto_create_session=True)


async def create_adk_session(runner: Runner, user_id: str, apartment: str) -> str:
    """Cria a sessão do morador com o apartamento fixo no estado (Garantia 2)."""
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, state={"apartamento": apartment}
    )
    return session.id


async def anchor_root(runner: Runner, user_id: str, session_id: str) -> None:
    """Re-fixa a sessão no principal AO FIM de um turno resolvido.

    Anexa um evento sintético `author=main_agent` para que o router devolva a
    raiz na próxima mensagem do morador. NÃO se ancora quando o último evento
    já é do principal (nada a fazer) nem quando a sessão está vazia.

    O timestamp precisa ser ESTRITAMENTE maior que o do maior evento já
    gravado: o `DatabaseSessionService` ordena por `(timestamp DESC, id DESC)`
    e o id é um UUID aleatório, então dois eventos com o mesmo timestamp (ou
    o `now` do relógio caindo no mesmo instante do último evento do turno)
    ficam em ordem arbitrária — o router podia então reler o especialista no
    lugar da raiz, o que era a origem da intermitência dos passos 11/12/13.
    Ancorar em `max(timestamps) + 1e-3` torna o evento sintético durável e
    sempre último.

    Quem chama DEVE garantir que o turno não terminou pedindo confirmação:
    nesse caso o último evento precisa continuar sendo o
    `adk_request_confirmation` do especialista para que a retomada chegue a
    ele (ver docstring do módulo e `api/main.py`).
    """
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id)
    if session is None or not session.events:
        return
    if session.events[-1].author == MAIN_AGENT_NAME:
        return
    max_ts = max((ev.timestamp or 0.0) for ev in session.events)
    await runner.session_service.append_event(
        session, Event(author=MAIN_AGENT_NAME, timestamp=max_ts + 1e-3))
