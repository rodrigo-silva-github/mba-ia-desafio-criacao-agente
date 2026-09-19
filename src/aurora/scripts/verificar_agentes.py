"""Verificação da Fase 3 (agentes + tools + confirmação + sessão persistida).

Uso:  uv run python -m aurora.scripts.verificar_agentes

Sem chave de API (usa o modelo fake). Corri sobre bancos temporários em
var/verif_* e valida de ponta a ponta sobre o App real:

  Sessão 101 (fluxo reservas — o mesmo que o avaliador exercita):
    T1 reserva aprovada   (confirmação AVANÇADA)  -> grava no store
    T2 reserva denegada                            -> não grava
    T3 cancelamento      (confirmação booleana)    -> cancela
    T4 reinício do runner (mesma sessão)           -> persistencia (G3)
    T5 disponibilidad                              -> esta_libre
  Sessão 302 (especialistas não-reservas, sessão LIMPIA):
    T6 visitante                                   -> autorizar + listar
    T7 regulamento                                 -> resposta com capítulos

Nota de comportamento ADK 2.9.2 (documentada): depois de RESOLVER uma
confirmação, a sessão continua a invocação do sub-agente single_turn — os
próximos textos do morador entram no mesmo especialista. Por eso o fluxo do
enunciado encaja: em cada /chat os pedidos do apartamento são todos de
reservas. Visitas/regulamento se testam em sessão limpa (como fará o
avaliador em apartamentos distintos).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Bancos temporários: devem apuntar a var/verif_* ANTES de importar aurora
# (config lee o env ao importar).
_RAIZ = Path(__file__).resolve().parent.parent.parent
os.environ["CAMINHO_BANCO_DADOS"] = str(_RAIZ / "var" / "verif_dados.db")
os.environ["CAMINHO_BANCO_SESSOES"] = str(_RAIZ / "var" / "verif_sessoes.db")

from google.adk.events import Event  # noqa: E402
from google.genai import types  # noqa: E402

from ..agentes import construir_app, criar_sessao, novo_runner  # noqa: E402
from ..agentes.confirmaciones import (  # noqa: E402
    achar_pendente,
    mensagem_texto,
    resposta_confirmacao,
)
from ..scripts.restore import restaurar  # noqa: E402
from ..storage import reservas_store, visitantes_store  # noqa: E402

FALLOS: list[str] = []
CONTEO: int = 0


def check(condicion: bool, etiqueta: str) -> None:
  global CONTEO
  CONTEO += 1
  if condicion:
    print(f"  [ok] {etiqueta}")
  else:
    print(f"  [FALLO] {etiqueta}")
    FALLOS.append(etiqueta)


async def turno(runner, sid: str, content: types.Content) -> list[Event]:
  eventos: list[Event] = []
  async for ev in runner.run_async(user_id=USER, session_id=sid, new_message=content):
    eventos.append(ev)
  return eventos


def fr_respuestas(eventos: list[Event]) -> list[tuple[str, dict]]:
  """(nome, response) de cada FunctionResponse do turno (content pode ser None)."""
  salida: list[tuple[str, dict]] = []
  for ev in eventos:
    contenido = ev.content
    if contenido is None or contenido.parts is None:
      continue
    for part in contenido.parts:
      fr = part.function_response
      if fr is not None:
        salida.append((fr.name, fr.response))
  return salida


def pendente_de(eventos: list[Event], tool: str):
  """Pendência cuja tool original é `tool`; None se não houver."""
  pend = achar_pendente(eventos)
  if pend is None or pend.original.get("name") != tool:
    return None
  return pend


async def sessao_101(app) -> None:
  global USER
  USER = "morador-101"
  runner = novo_runner(app)
  sid = await criar_sessao(runner, USER, "101")

  # ---- T1: reserva aprovada (confirmação avanzada) ----------------------
  print("\n== T1: reserva aprovada (confirmação AVANÇADA) ==")
  evs = await turno(runner, sid, mensagem_texto("quero reservar a quadra para 2030-05-02"))
  pend = pendente_de(evs, "reservar_area")
  check(pend is not None, "se crió uma pendência de reserva")
  if pend:
    print(f"    id={pend.id_para_responder[:12]} acao={pend.acao!r}")
    check("cobrança" in pend.acao, "o hint menciona a cobrança")
    evs2 = await turno(runner, sid, resposta_confirmacao(
        pend.id_para_responder, True,
        {"area": "quadra", "data": "2030-05-02", "apartamento": "101"}))
    del evs2
    check(not await reservas_store.esta_libre("quadra", "2030-05-02"),
          "quadra 2030-05-02 ficou reservada")
    check(len(await reservas_store.listar_apartamento("101")) == 2,
          "o 101 tem 2 reservas (seed + nova)")

  # ---- T2: reserva denegada ---------------------------------------------
  print("\n== T2: reserva denegada ==")
  evs = await turno(runner, sid, mensagem_texto("quero reservar a piscina para 2030-05-03"))
  pend = pendente_de(evs, "reservar_area")
  check(pend is not None, "se crió uma pendência (piscina)")
  if pend:
    await turno(runner, sid, resposta_confirmacao(pend.id_para_responder, False))
    check(await reservas_store.esta_libre("piscina", "2030-05-03"),
          "piscina 2030-05-03 ficou livre (não se gravó)")

  # ---- T3: cancelamento (confirmação booleana) ---------------------------
  print("\n== T3: cancelamento próprio (confirmação booleana) ==")
  evs = await turno(runner, sid, mensagem_texto("quero cancelar a reserva RSV-1377"))
  pend = pendente_de(evs, "cancelar_reserva")
  check(pend is not None, "se crió uma pendência de cancelamento")
  if pend:
    await turno(runner, sid, resposta_confirmacao(pend.id_para_responder, True))
    check(await reservas_store.cancelar("101", "RSV-1377") is False,
          "RSV-1377 já não está activa (cancelamento feito)")

  # ---- T4: reinício do runner (mesma sessão, Garantia 3) -----------------
  print("\n== T4: reinício do runner (mesma sessão persistida) ==")
  runner2 = novo_runner(app)
  evs = await turno(runner2, sid, mensagem_texto("quais são as minhas reservas"))
  frs = fr_respuestas(evs)
  check(any(nome == "listar_reservas" for nome, _ in frs),
        "listado de reservas após reinício (tool executada)")
  res = [r for n, r in frs if n == "listar_reservas"]
  check(res and res[0].get("apartamento") == "101", "o listado é do apartamento 101")

  # ---- T5: disponibilidad --------------------------------------------------
  print("\n== T5: disponibilidad ==")
  await turno(runner2, sid, mensagem_texto("está livre a quadra para 2030-05-02?"))
  check(not await reservas_store.esta_libre("quadra", "2030-05-02"),
        "quadra 2030-05-02 -> ocupada (store)")


async def sessao_302(app) -> None:
  global USER
  USER = "morador-302"
  runner = novo_runner(app)
  sid = await criar_sessao(runner, USER, "302")

  # ---- T6: visitante (sessão limpa) --------------------------------------
  print("\n== T6: visitante (sessão limpa) ==")
  evs = await turno(runner, sid, mensagem_texto("quero autorizar o visitante Carlos Diaz para 2030-06-01"))
  frs = fr_respuestas(evs)
  res = [r for n, r in frs if n == "autorizar_visita"]
  check(res and res[0].get("status") == "ok", "visitante autorizado")
  visitantes = await visitantes_store.listar_apartamento("302")
  check(any(v["nome"].lower() == "carlos diaz" for v in visitantes), "Carlos Diaz listado no 302")

  # ---- T7: regulamento (sessão limpa) --------------------------------------
  print("\n== T7: regulamento (sessão limpa) ==")
  evs = await turno(runner, sid, mensagem_texto("que regras existem para a piscina?"))
  frs = fr_respuestas(evs)
  res = [r for n, r in frs if n == "consultar_regulamento"]
  check(res and "Capítulo IV" in res[0].get("resposta", ""),
        "a resposta inclui o Capítulo IV (Piscina)")


async def principal() -> None:
  for sufixo in ("", "-wal", "-shm"):
    for base in ("var/verif_dados.db", "var/verif_sessoes.db"):
      p = str(_RAIZ / base) + sufixo
      if os.path.exists(p):
        os.remove(p)
  await restaurar()
  print("== seeds restaurados (bancos de verificación) ==")

  app = construir_app()
  await sessao_101(app)
  await sessao_302(app)


def main() -> None:
  try:
    asyncio.run(principal())
  except BaseException:
    import traceback

    traceback.print_exc()
    sys.exit(1)
  finally:
    print(f"\nRESULTADO: {CONTEO} checks, {len(FALLOS)} falhas")
    sys.exit(1 if FALLOS else 0)


if __name__ == "__main__":
  main()