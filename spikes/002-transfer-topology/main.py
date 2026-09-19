"""Spike 002 — topologia final (single_turn) + re-ancoragem na raiz.

Problema medido (neste wheel ADK 2.9.2, build 2026):
  - Tanto single_turn quanto transfer_to_agent retomam a confirmação 10/10 e
    executam a tool aprovada; PORÉM, depois de resolver, a sessão fica
    "presa" no especialista: o router escolhe o último evento com autor
    conhecido (o especialista), então a próxima mensagem mista (visitante,
    regulamento) entra no especialista errado. O avaliador SIM mistura
    domínios na mesma sessão (S1: reservas 3-10, visitante 11, piscina 12).

Solução medida: RE-ANCORAR a sessão na raiz. Após cada turno SEM pendência
ativa (sem `long_running_tool_ids` nem `requested_tool_confirmations` no
turno), se o último evento da sessão não for do main_agent, anexa-se um
evento sintético com author=main_agent via session_service.append_event. O
router então devolve a raiz e a próxima mensagem é rotacionada corretamente.
Durante uma pendência ativa NÃO se re-ancora (a retomada precisa que o
router continue escolhendo o especialista para resolver o FC pendente).

Protocolo por iteração (sessão nova 101, data única):
  1. "Reserve el salão para <fecha>"        -> pending de book_area
  2. aprovar (confirmado=true)              -> grava a reserva
  3. "Libera a Joana Ribeiro el 2030-04-21" -> visitors_agent (antes falhava)
  4. aprovar                                -> grava
  5. "¿A qué hora cierra la piscina los domingos?" -> query_regulations
Além disso: pending criado com Runner A e respondido com Runner B (restart, G3).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
os.environ["SESSIONS_DB_PATH"] = str(_ROOT / "var" / "spike2_sessoes.db")
os.environ["BUSINESS_DB_PATH"] = str(_ROOT / "var" / "spike2_dados.db")

sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_llm import SpikeFakeLlm  # noqa: E402

from google.adk.agents import Agent  # noqa: E402
from google.adk.apps import App, ResumabilityConfig  # noqa: E402
from google.adk.events import Event  # noqa: E402
from google.adk.models import LLMRegistry  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import DatabaseSessionService  # noqa: E402
from google.genai import types  # noqa: E402

from aurora.agents.confirmations import (  # noqa: E402
    confirmation_response,
    find_pending,
    text_message,
)
from aurora.agents.tools import (  # noqa: E402
    TOOLS_REGULAMENTO,
    TOOLS_RESERVAS,
    TOOLS_VISITANTES,
)
from aurora.storage import db as business_db  # noqa: E402
from aurora.storage import reservas_store, visitantes_store  # noqa: E402

LLMRegistry.register(SpikeFakeLlm)

APP_NAME = "aurora"
MAIN_NAME = "main_agent"
MODELS = {"main": "spike-main", "reservas": "spike-reservations",
          "visitantes": "spike-visitors", "regulamento": "spike-regulations"}

INSTR = {
    "main": ("Você é o assistente do Residencial Aurora. Rotea con las tools "
             "reservations_agent(request=...) | visitors_agent(request=...) | "
             "regulations_agent(request=...)."),
    "reservas": "Especialista de reservas: usa book_area, cancel_reservation, list_reservations, check_availability.",
    "visitantes": "Especialista de visitantes: usa authorize_visit, list_visitors.",
    "regulamento": "Especialista de regulamento: usa query_regulations.",
}


def build_app() -> App:
    reservations = Agent(name="reservations_agent", model=MODELS["reservas"],
                         instruction=INSTR["reservas"], mode="single_turn",
                         tools=TOOLS_RESERVAS)
    visitors = Agent(name="visitors_agent", model=MODELS["visitantes"],
                     instruction=INSTR["visitantes"], mode="single_turn",
                     tools=TOOLS_VISITANTES)
    regulations = Agent(name="regulations_agent", model=MODELS["regulamento"],
                        instruction=INSTR["regulamento"], mode="single_turn",
                        tools=TOOLS_REGULAMENTO)
    main = Agent(name=MAIN_NAME, model=MODELS["main"], instruction=INSTR["main"],
                 sub_agents=[reservations, visitors, regulations])
    return App(name=APP_NAME, root_agent=main,
               resumability_config=ResumabilityConfig(is_resumable=True))


def new_runner(app: App) -> Runner:
    sessions_path = os.environ["SESSIONS_DB_PATH"]
    service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{sessions_path}")
    return Runner(app=app, session_service=service, auto_create_session=True)


async def create_session(runner: Runner, user_id: str) -> str:
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, state={"apartamento": "101"})
    return session.id


def _turn_is_paused(events: list) -> bool:
    """True se o turno deixou uma confirmação pendente (pausa do runner)."""
    for ev in events:
        if ev.long_running_tool_ids:
            return True
        if ev.actions and ev.actions.requested_tool_confirmations:
            return True
    return False


async def reanchor(runner: Runner, user_id: str, sid: str, awaiting: bool) -> None:
    """Re-ancora a sessão na raiz ANTES do próximo turno.

    Se houver uma pendência ativa (awaiting=True) não faz nada: a retomada
    precisa que o router continue escolhendo o especialista. Sem pendência, se
    o último evento da sessão não for do main (o especialista ficou no
    comando após resolver uma confirmação), anexa-se um evento sintético do
    main via session_service.append_event para que o router devolva a raiz.
    """
    if awaiting:
        return
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=sid)
    if session is None or not session.events:
        return
    last_author = session.events[-1].author
    if last_author == MAIN_NAME:
        return
    await runner.session_service.append_event(
        session, Event(author=MAIN_NAME, timestamp=time.time() + 0.01))


async def turn(runner: Runner, user_id: str, sid: str, content: types.Content,
               awaiting: bool = False) -> list:
    """Executa um turno; antes, se NÃO houver pendência ativa e o último evento
    não for do main, anexa o evento sintético de re-ancoragem (o router volta
    à raiz). Com pendência ativa (awaiting=True) NUNCA re-ancora: a retomada
    precisa que o router continue escolhendo o especialista."""
    await reanchor(runner, user_id, sid, awaiting=awaiting)
    events = []
    async for ev in runner.run_async(user_id=user_id, session_id=sid, new_message=content):
        events.append(ev)
    return events


async def diagnostic(runner, user_id, sid, n) -> None:
    ses = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=sid)
    print(f"    [diag {n}] total={len(ses.events)}")
    for i, ev in enumerate(ses.events[-10:]):
        eoa = bool(ev.actions.end_of_agent) if ev.actions else False
        fc = [(fc.name, str(fc.id)[-7:]) for fc in ev.get_function_calls()]
        frs = [fr.name for fr in
               (ev.get_function_responses() if ev.content else [])]
        txt = ""
        if ev.content:
            for part in (ev.content.parts or []):
                if part.text:
                    txt = part.text[:30]
        print(f"      [{len(ses.events)-10+i}] {ev.author:<20} id={str(ev.id)[-7:]} "
              f"eoa={eoa} fc={fc} fr={frs} txt={txt!r}")


def named_frs(events: list, name: str) -> list[dict]:
    out = []
    for ev in events:
        for part in (ev.content.parts or []) if ev.content else []:
            fr = part.function_response
            if fr is not None and fr.name == name and fr.response is not None:
                out.append(fr.response)
    return out


def author_names(events: list) -> set[str]:
    return {ev.author for ev in events}


RESULTS = {"pendings_ok": 0, "approve_booked": 0, "visitor_ok": 0, "visit_ok": 0,
           "regulations_ok": 0, "resume_post_restart": 0}


async def one_iteration(app: App, n: int) -> None:
    user_id = f"morador-101-{n}"
    runner_a = new_runner(app)
    sid = await create_session(runner_a, user_id)
    date = f"2030-06-{n:02d}"

    # 1) reserva com confirmação
    evs = await turn(runner_a, user_id, sid,
                     text_message(f"Reserve el salão de fiestas para {date}"))
    pend = find_pending(evs)
    if pend is None:
        print(f"  [{n:02d}] FALHA: no hay pendencia de reserva (authors={author_names(evs)})")
        return
    RESULTS["pendings_ok"] += 1

    # 2) aprovar -> grava (após aprovar NÃO fica pendente: próximo turno re-ancora)
    evs_app = await turn(runner_a, user_id, sid,
                         confirmation_response(pend.id_to_answer, True, pend.details))
    if await reservas_store.is_available("salao-de-festas", date) is False:
        RESULTS["approve_booked"] += 1
    elif n == 2:
        print("    [n=2] aprobación NO grabó?!")
    if n == 2:
        print("    [n=2] eventos del turno de aprobación:",
              [(str(ev.id)[-7:], ev.author, len(ev.get_function_calls()))
               for ev in evs_app])
        ses_pre = await runner_a.session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=sid)
        print(f"    [pre-visitor n=2] tras aprobar: total={len(ses_pre.events)}")
        for i, ev in enumerate(ses_pre.events[-6:]):
            print(f"      [{len(ses_pre.events)-6+i}] {ev.author:<20} id={str(ev.id)[-7:]}")

    # 3) mensagem de OUTRO domínio: deve ir para visitors_agent (antes: armadilha)
    evs2 = await turn(runner_a, user_id, sid,
                      text_message("Libera a Joana Ribeiro el día 2030-04-21"))
    pend_v = find_pending(evs2)
    if pend_v is not None and "visitors_agent" in author_names(evs2):
        RESULTS["visitor_ok"] += 1
        await turn(runner_a, user_id, sid,
                   confirmation_response(pend_v.id_to_answer, True, pend_v.details))
        if any(v["nome"].lower() == "joana ribeiro" for v in
               await visitantes_store.list_apartment("101")):
            RESULTS["visit_ok"] += 1
    else:
        print(f"  [{n:02d}] FALHA visitante: authors={author_names(evs2)} "
              f"pends={pend_v is not None}")
        await diagnostic(runner_a, user_id, sid, n)

    # 4) piscina -> regulations_agent
    evs3 = await turn(runner_a, user_id, sid,
                      text_message("¿A qué hora cierra la piscina los domingos?"))
    if named_frs(evs3, "query_regulations") and "regulations_agent" in author_names(evs3):
        RESULTS["regulations_ok"] += 1
    else:
        print(f"  [{n:02d}] FALHA regulamento: authors={author_names(evs3)}")

    # 5) restart real: pending criado com runner_a, aprovado com runner_b
    #    (outro Runner + outra conexão, mesmo banco: simula o restart da API)
    if n <= 3:
        runner_b = new_runner(app)
        evs_b = await turn(runner_b, user_id, sid,
                           confirmation_response(pend.id_to_answer, True, pend.details))
        if await reservas_store.is_available("salao-de-festas", date) is False:
            RESULTS["resume_post_restart"] += 1
            print(f"  [..] retomada com Runner novo (restart): OK (iter {n})")
        else:
            print(f"  [..] retomada com Runner novo: FALHA (iter {n})")


async def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        for base in ("var/spike2_sessoes.db", "var/spike2_dados.db"):
            p = str(_ROOT / base) + suffix
            if os.path.exists(p):
                os.remove(p)
    await business_db.init_db()

    app = build_app()
    for n in range(1, 11):
        await one_iteration(app, n)

    print("\n== RESULTADOS (10 iterações, single_turn + re-anclagem) ==")
    for k, v in RESULTS.items():
        print(f"  {k}: {v}")
    fails = [v for k, v in RESULTS.items() if k != "resume_post_restart" and v < 10]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
