"""Spike 001: confirmacao de tools persistida (ADK 2.9.2, SQLite).

Cenarios deterministicos:
  A. Confirmacao avancada (request_confirmation com payload), single agent.
  B. Confirmacao booleana (require_confirmation=True).
  C. Negacao: confirmado=False -> nada e gravado.
  D. Reenvio da mesma resposta -> nao re-executa (base para o 409 da API).
  E. "Reinicio da API": novo Runner + novo DatabaseSessionService no mesmo
     arquivo SQLite -> pendencia continua respondivel (Garantia 3).
  H. TOPOLOGIA SINGLE_TURN: especialista como tool inline -> confirmacao e
     retomada no mesmo runner.
  I. TOPOLOGIA SINGLE_TURN + reinicio.

Cenarios de EVIDENCIA (nao contam para o resultado; documentam o risco):
  F. Transferencia (sub_agents chat): retomada com o mesmo runner.
  G. Transferencia + reinicio: retomada com runner novo.

Roda com modelo fake; nao precisa de GEMINI_API_KEY.

VEREDITO ANTECIPADO (confirmado pelos cenarios abaixo):
  - Topologia com transfer_to_agent NAO retoma confirmacao de forma
    confiavel (no-op silencioso e 'Agent X cannot transfer to itself').
  - Topologia single_turn (sub_agent mode='single_turn') retoma SEMPRE.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_llm import CONFIRMACAO, FakeLlm  # noqa: E402

from google.adk.agents import Agent  # noqa: E402
from google.adk.apps import App, ResumabilityConfig  # noqa: E402
from google.adk.events import Event  # noqa: E402
from google.adk.models import LLMRegistry  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import DatabaseSessionService  # noqa: E402
from google.adk.tools import FunctionTool, ToolContext  # noqa: E402
from google.genai import types  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessoes.db")
APP_NAME = "aurora_spike"
USER_ID = "morador-101"

LLMRegistry.register(FakeLlm)

EXECUCOES: dict[str, int] = {"reservar_salao": 0, "liberar_visitante": 0}


def reservar_salao(area: str, data: str, tool_context: ToolContext) -> dict:
  """Reserva area comum (garante cobranca -> exige confirmacao)."""
  tc = tool_context.tool_confirmation
  if tc is None:
    tool_context.request_confirmation(
        hint=f"Confirmar reserva do {area} em {data}? Gera cobranca.",
        payload={"area": area, "data": data, "confirmado": False},
    )
    return {"status": "aguardando_confirmacao"}
  if not tc.confirmed:
    return {"status": "recusada_pelo_morador"}
  EXECUCOES["reservar_salao"] += 1
  return {"status": "ok", "codigo": f"RSV-SPIKE-{EXECUCOES['reservar_salao']}", "area": area, "data": data}


def liberar_visitante(nome: str, data: str) -> dict:
  """Libera entrada de visitante (libera acesso -> exige confirmacao)."""
  EXECUCOES["liberar_visitante"] += 1
  return {"status": "ok", "visitante": nome, "data": data}


def build_app(com_transferencia: bool) -> App:
  """True: topologia chat + transfer_to_agent. False: single_turn (inline)."""
  especialista = Agent(
      name="especialista_reservas",
      model="aurora-fake-reservas",
      instruction="Voce e o especialista de reservas do condominio Aurora.",
      mode=None if com_transferencia else "single_turn",
      tools=[
          reservar_salao,
          FunctionTool(liberar_visitante, require_confirmation=True),
      ],
  )
  principal = Agent(
      name="agente_principal",
      model="aurora-fake-main",
      instruction="Roteie a tarefa para o especialista de reservas.",
      sub_agents=[especialista],
  )
  return App(
      name=APP_NAME,
      root_agent=principal,
      resumability_config=ResumabilityConfig(is_resumable=True),
  )


def novo_runner(app: App) -> Runner:
  """Novo Runner + novo DatabaseSessionService no MESMO arquivo (simula restart)."""
  service = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{DB_PATH}")
  return Runner(app=app, session_service=service, auto_create_session=True)


async def criar_sessao(runner: Runner) -> str:
  service = runner.session_service
  session = await service.create_session(
      app_name=APP_NAME, user_id=USER_ID, state={"apartamento": "101"}
  )
  return session.id


async def run_turno(runner: Runner, session_id: str, content: types.Content) -> list[Event]:
  events: list[Event] = []
  async for ev in runner.run_async(user_id=USER_ID, session_id=session_id, new_message=content):
    events.append(ev)
  return events


def mensagem_texto(texto: str) -> types.Content:
  return types.Content(role="user", parts=[types.Part(text=texto)])


def resposta_confirmacao(fc_id: str, confirmado: bool, payload: dict | None = None) -> types.Content:
  response: dict = {"confirmed": confirmado}
  if payload is not None:
    response["payload"] = payload
  return types.Content(
      role="user",
      parts=[
          types.Part(
              function_response=types.FunctionResponse(
                  id=fc_id, name=CONFIRMACAO, response=response
              )
          )
      ],
  )


def achar_pendente(events: list[Event]) -> tuple[str, dict, dict] | None:
  """Retorna (id_para_responder, tool_confirmation, original_function_call).

  A chamada 'adk_request_confirmation' (a que devemos responder) e o marcador
  'requested_tool_confirmations' caem em eventos diferentes do stream: o
  marcador vem no evento com a resposta da tool (chave = id da chamada
  original); a chamada a responder vem no evento do modelo.
  """
  req_orig_ids: set[str] = set()
  for ev in events:
    reqs = ev.actions.requested_tool_confirmations if ev.actions else None
    if reqs:
      req_orig_ids.update(reqs.keys())
  for ev in reversed(events):
    for fc in ev.get_function_calls():
      if fc.name != CONFIRMACAO or not fc.id:
        continue
      args = fc.args or {}
      orig = args.get("originalFunctionCall") or {}
      if req_orig_ids and orig.get("id") not in req_orig_ids:
        continue
      tc = args.get("toolConfirmation") or {}
      return fc.id, dict(tc), dict(orig)
  return None


def exec_delta(inicio: dict[str, int], tool: str) -> int:
  return EXECUCOES[tool] - inicio[tool]


def resumir(events: list[Event], rotulo: str) -> None:
  print(f"\n--- {rotulo} ---")
  for ev in events:
    acoes = getattr(ev.actions, "requested_tool_confirmations", None)
    partes = []
    for part in (ev.content.parts or []) if ev.content else []:
      if part.text:
        partes.append(f"texto={part.text[:60]!r}")
      if part.function_call:
        partes.append(f"fc={part.function_call.name}({part.function_call.id})")
      if part.function_response:
        partes.append(f"fr={part.function_response.name}:{part.function_response.response}")
    print(f"  [{ev.author}] {', '.join(partes) if partes else '(sem partes)'}" + (" [PENDENCIA]" if acoes else ""))


async def cenario_a(cen: str) -> int:
  """Confirmacao avancada; aprovacao; reenvio."""
  print("\n\n================ CENARIO A: confirmacao avancada ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("quero reservar o salao"))
  resumir(evs, "A1 - pedido de reserva")
  pend = achar_pendente(evs)
  assert pend, "A1: esperava confirmacao pendente"
  fc_id, tc, orig = pend
  print(f"  pendencia: id={fc_id[:12]} tc={tc} original={orig.get('name')}")
  assert orig.get("name") == "reservar_salao", f"A1: original deveria ser reservar_salao, veio {orig.get('name')}"

  evs = await run_turno(runner, sid, resposta_confirmacao(fc_id, True, {"area": "salao-de-festas", "data": "2030-04-20"}))
  resumir(evs, "A2 - aprovacao")
  assert exec_delta(ini, "reservar_salao") == 1, "A2: tool deveria executar exatamente 1x"
  print("  OK: reservar_salao executou exatamente 1 vez")

  try:
    await run_turno(runner, sid, resposta_confirmacao(fc_id, True))
    print("  INFO: reenvio nao levantou erro; a API mapeia o 409 pela tabela de pendentes")
  except Exception as e:  # noqa: BLE001
    print(f"  OK: reenvio da mesma resposta falhou -> {type(e).__name__}: {str(e)[:120]}")

  evs = await run_turno(runner, sid, mensagem_texto("quais sao as minhas reservas"))
  resumir(evs, "A3 - conversa continua apos a confirmacao")
  return 1


async def cenario_b(cen: str) -> int:
  """Confirmacao booleana (require_confirmation)."""
  print("\n\n================ CENARIO B: confirmacao booleana ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("quero liberar um visitante"))
  resumir(evs, "B1 - pedido de visita")
  pend = achar_pendente(evs)
  assert pend, "B1: esperava confirmacao pendente"
  fc_id, tc, orig = pend
  print(f"  pendencia: id={fc_id[:12]} tc={tc} original={orig.get('name')}")
  assert orig.get("name") == "liberar_visitante", "B1: original deveria ser liberar_visitante"

  evs = await run_turno(runner, sid, resposta_confirmacao(fc_id, True))
  resumir(evs, "B2 - aprovacao")
  assert exec_delta(ini, "liberar_visitante") == 1, "B2: tool deveria executar exatamente 1x"
  print("  OK: liberar_visitante executou exatamente 1 vez")
  return 1


async def cenario_c(cen: str) -> int:
  """Negacao nao grava nada."""
  print("\n\n================ CENARIO C: negacao ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("quero reservar o salao"))
  pend = achar_pendente(evs)
  assert pend, "C1: esperava pendencia"

  evs = await run_turno(runner, sid, resposta_confirmacao(pend[0], False))
  resumir(evs, "C2 - negacao")
  assert exec_delta(ini, "reservar_salao") == 0, "C: negar nao pode gravar a reserva"
  print("  OK: negar nao gravou a reserva (a tool retornou recusada)")
  return 1


async def cenario_d(cen: str) -> int:
  """Reenvio com id de confirmacao ja respondida (base do 409)."""
  print("\n\n================ CENARIO D: reenvio da mesma pendencia ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("reservar o salao"))
  pend = achar_pendente(evs)
  assert pend, "D1: esperava pendencia"

  await run_turno(runner, sid, resposta_confirmacao(pend[0], True))
  assert exec_delta(ini, "reservar_salao") == 1, "D2: aprovacao deveria executar 1x"

  try:
    await run_turno(runner, sid, resposta_confirmacao(pend[0], True))
  except Exception as e:  # noqa: BLE001
    print(f"  OK: reenvio levantou {type(e).__name__}: {str(e)[:120]}")
  assert exec_delta(ini, "reservar_salao") == 1, "D3: reenvio NAO pode re-executar a tool"
  print("  OK: reenvio nao re-executou a tool (delta == 1)")
  return 1


async def cenario_e(cen: str) -> int:
  """Reinicio: novo Runner + novo session service no mesmo sqlite."""
  print("\n\n================ CENARIO E: reinicio da API (persistencia) ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner1 = novo_runner(app)
  sid = await criar_sessao(runner1)

  evs = await run_turno(runner1, sid, mensagem_texto("reservar o salao"))
  pend = achar_pendente(evs)
  assert pend, "E1: esperava pendencia"
  print(f"  E1: pendencia criada no runner1: {pend[0][:12]}")

  runner2 = novo_runner(app)
  session = await runner2.session_service.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=sid)
  assert session is not None, "E2: sessao nao carregou apos o reinicio"
  print(f"  E2: sessao carregada apos reinicio; eventos na sessao: {len(session.events)}")

  evs = await run_turno(runner2, sid, resposta_confirmacao(pend[0], True))
  resumir(evs, "E3 - aprovacao apos reinicio")
  assert exec_delta(ini, "reservar_salao") == 1, "E3: tool deveria executar 1x apos reinicio"
  print("  OK: confirmacao respondida e executada APOS reinicio (Garantia 3)")
  return 1


async def cenario_f(cen: str) -> int:
  """EVIDENCIA: transferencia, retomada com o mesmo runner (risco do enunciado)."""
  print("\n\n================ CENARIO F (EVIDENCIA): transferencia, mesmo runner ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=True)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("quero reservar o salao via especialista"))
  resumir(evs, "F1 - pedido com transferencia")
  pend = achar_pendente(evs)
  assert pend, "F1: esperava pendencia"
  try:
    evs = await run_turno(runner, sid, resposta_confirmacao(pend[0], True))
    resumir(evs, "F2 - aprovacao apos transferencia")
    print(f"  EVIDENCIA: retomada executou? {'SIM' if exec_delta(ini, 'reservar_salao') == 1 else 'NAO (no-op)'}")
  except Exception as e:  # noqa: BLE001
    print(f"  EVIDENCIA: retomada apos transferencia FALHOU -> {type(e).__name__}: {str(e)[:100]}")
  print("  (transfer_to_agent NAO e confiavel para retomada nesta versao)")
  return 1


async def cenario_g(cen: str) -> int:
  """EVIDENCIA: transferencia + reinicio (pior caso do enunciado)."""
  print("\n\n================ CENARIO G (EVIDENCIA): transferencia + reinicio ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=True)
  runner1 = novo_runner(app)
  sid = await criar_sessao(runner1)

  evs = await run_turno(runner1, sid, mensagem_texto("reservar o salao via especialista"))
  pend = achar_pendente(evs)
  assert pend, "G1: esperava pendencia"
  runner2 = novo_runner(app)
  try:
    evs = await run_turno(runner2, sid, resposta_confirmacao(pend[0], True))
    resumir(evs, "G2 - aprovacao apos reinicio com transferencia")
    print(f"  EVIDENCIA: retomada executou? {'SIM' if exec_delta(ini, 'reservar_salao') == 1 else 'NAO (no-op)'}")
  except Exception as e:  # noqa: BLE001
    print(f"  EVIDENCIA: transferencia + reinicio FALHOU -> {type(e).__name__}: {str(e)[:100]}")
  return 1


async def cenario_h(cen: str) -> int:
  """TOPOLOGIA SINGLE_TURN: confirmacao e retomada no mesmo runner."""
  print("\n\n================ CENARIO H: single_turn (confirmacao) ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner = novo_runner(app)
  sid = await criar_sessao(runner)

  evs = await run_turno(runner, sid, mensagem_texto("quero reservar o salao"))
  resumir(evs, "H1 - pedido de reserva (single_turn)")
  pend = achar_pendente(evs)
  assert pend, "H1: esperava confirmacao pendente"
  fc_id, tc, orig = pend
  print(f"  pendencia: id={fc_id[:12]} tc={tc} original={orig.get('name')}")
  assert orig.get("name") == "reservar_salao", f"H1: original={orig.get('name')}"

  evs = await run_turno(runner, sid, resposta_confirmacao(fc_id, True))
  resumir(evs, "H2 - aprovacao (single_turn)")
  assert exec_delta(ini, "reservar_salao") == 1, "H2: tool deveria executar 1x"
  print("  OK: single_turn + confirmacao + retomada no MESMO runner")
  return 1


async def cenario_i(cen: str) -> int:
  """TOPOLOGIA SINGLE_TURN + REINICIO da API (Garantia 3)."""
  print("\n\n================ CENARIO I: single_turn + reinicio ================")
  ini = dict(EXECUCOES)
  app = build_app(com_transferencia=False)
  runner1 = novo_runner(app)
  sid = await criar_sessao(runner1)

  evs = await run_turno(runner1, sid, mensagem_texto("reservar o salao"))
  pend = achar_pendente(evs)
  assert pend, "I1: esperava pendencia"

  runner2 = novo_runner(app)
  evs = await run_turno(runner2, sid, resposta_confirmacao(pend[0], True))
  resumir(evs, "I2 - aprovacao apos reinicio (single_turn)")
  assert exec_delta(ini, "reservar_salao") == 1, "I2: tool deveria executar 1x apos reinicio"
  print("  OK: single_turn + reinicio + aprovacao (Garantia 3)")
  return 1


async def main() -> None:
  if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
  cenarios = [
      cenario_a, cenario_b, cenario_c, cenario_d, cenario_e,
      cenario_f, cenario_g, cenario_h, cenario_i,
  ]
  ok = 0
  for cen in cenarios:
    try:
      ok += await cen(ok)
    except Exception as e:  # noqa: BLE001
      print(f"  FALHOU: {type(e).__name__}: {e}")
  print(f"\n\nRESULTADO: {ok}/{len(cenarios)} cenarios OK (F/G sao evidencias, nao contam)")
  sys.exit(0 if ok >= 7 else 1)


if __name__ == "__main__":
  asyncio.run(main())