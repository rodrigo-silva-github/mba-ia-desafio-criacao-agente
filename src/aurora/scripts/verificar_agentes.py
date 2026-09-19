"""Verificação da Fase 4 (agentes + tools + confirmação + roteamento).

Uso:  uv run python -m aurora.scripts.verificar_agentes

Sem chave de API (usa o modelo fake). Corre sobre bancos temporários em
var/verif_* e valida de ponta a ponta sobre o App real, no MESMO nível que
a API (um Runner novo por turno, como em `api/main.py`):

  Sessão 101 (fluxo misto na MESMA sessão — o caso que o avaliador exercita):
    T1 reserva aprovada   (confirmação AVANÇADA)      -> grava no store
    T2 reserva negada                                 -> não grava
    T3 cancelamento       (SEM confirmação, Fase 4)   -> cancela direto
    T4 reserva aprovada                               -> grava
    T5 piscina (regulamento) na mesma sessão          -> query_regulations
    T6 visitante "já confirmo aqui" -> pede confirmação (Fase 4) -> aprova
    T7 reinício do runner (mesma sessão)              -> persistência (G3)
  Sessão 302:
    T8 visita sem confirmação prévia -> pendência (Fase 4)
  Sessão 101 B:
    T9 salão 2030-03-16 (do 302) -> date_taken, SEM vazar RSV-4821
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Bancos temporários: devem apontar a var/verif_* ANTES de importar aurora
# (config lê o env ao importar).
_ROOT = Path(__file__).resolve().parent.parent.parent
os.environ["BUSINESS_DB_PATH"] = str(_ROOT / "var" / "verif_dados.db")
os.environ["SESSIONS_DB_PATH"] = str(_ROOT / "var" / "verif_sessoes.db")

from google.adk.events import Event  # noqa: E402
from google.genai import types  # noqa: E402

from ..agents import (  # noqa: E402
    anchor_root,
    build_app,
    confirmation_response,
    create_adk_session,
    find_pending,
    new_runner,
    text_message,
)
from ..scripts.restore import restore  # noqa: E402
from ..storage import reservas_store, visitantes_store  # noqa: E402

FAILS: list[str] = []
TOTAL: int = 0


def check(condicion: bool, label: str) -> None:
    global TOTAL
    TOTAL += 1
    if condicion:
        print(f"  [ok] {label}")
    else:
        print(f"  [FALHA] {label}")
        FAILS.append(label)


async def turn(runner, user_id: str, sid: str, content: types.Content) -> list[Event]:
    """Roda um turno igual ao que faz a API.

    Ao terminar SEM pendência, re-fixa a sessão no principal (`anchor_root`),
    exatamente como `api/main.py`.
    """
    events: list[Event] = []
    async for ev in runner.run_async(user_id=user_id, session_id=sid, new_message=content):
        events.append(ev)
    if find_pending(events) is None:
        await anchor_root(runner, user_id, sid)
    return events


def all_texts(events: list[Event]) -> str:
    """Concatena os textos de todos os eventos do turno (para vazamentos)."""
    out = []
    for ev in events:
        if ev.content and ev.content.parts:
            out += [p.text or "" for p in ev.content.parts if p.text]
    return "\n".join(out)


def pending_of(events: list[Event], tool: str):
    pend = find_pending(events)
    if pend is None or pend.original.get("name") != tool:
        return None
    return pend


async def session_101(app) -> None:
    user_id = "morador-101-verif"
    runner = new_runner(app)
    sid = await create_adk_session(runner, user_id, "101")
    await reservas_store.is_available("salao-de-festas", "2030-03-16")

    # ---- T1: reserva aprovada (confirmação AVANÇADA) -------------------
    print("\n== T1: reserva aprovada (confirmação) ==\n")
    evs = await turn(runner, user_id, sid, text_message("quero reservar a churrasqueira para 2030-05-02"))
    pend = pending_of(evs, "book_area")
    check(pend is not None, "se criou uma pendência de reserva")
    if pend:
        check("cobrança" in pend.action, "o hint menciona a cobrança")
        check(pend.details.get("area") == "churrasqueira"
              and pend.details.get("data") == "2030-05-02",
              "o payload leva area+data")
        await turn(runner, user_id, sid, confirmation_response(
            pend.id_to_answer, True, pend.details))
        check(not await reservas_store.is_available("churrasqueira", "2030-05-02"),
              "churrasqueira 2030-05-02 ficou reservada")
        r101 = await reservas_store.list_apartment("101")
        check(len(r101) == 2, f"o 101 tem 2 reservas (seed + nova; {len(r101)})")

    # ---- T2: reserva negada ------------------------------------------
    print("\n== T2: reserva negada ==\n")
    evs = await turn(runner, user_id, sid, text_message("quero reservar a churrasqueira para 2030-05-03"))
    pend = pending_of(evs, "book_area")
    check(pend is not None, "se criou uma pendência (churrasqueira)")
    if pend:
        await turn(runner, user_id, sid, confirmation_response(pend.id_to_answer, False))
        check(await reservas_store.is_available("churrasqueira", "2030-05-03"),
              "churrasqueira 2030-05-03 ficou livre (não se gravou)")

    # ---- T3: cancelamento SEM confirmação (fix Fase 4) -----------------
    print("\n== T3: cancelamento próprio (SEM confirmação, Fase 4) ==")
    evs = await turn(runner, user_id, sid, text_message("quero cancelar a reserva RSV-1377"))
    check(find_pending(evs) is None, "NÃO se criou pendência de cancelamento")
    check(await reservas_store.cancel("101", "RSV-1377") is False,
          "RSV-1377 já não está ativa (cancelada direto)")

    # ---- T4: reserva aprovada (salão) ------------------------------------
    print("\n== T4: reserva salão aprovada ==")
    evs = await turn(runner, user_id, sid, text_message("quero reservar o salão de festas para 2030-05-04"))
    pend = pending_of(evs, "book_area")
    check(pend is not None, "se criou pendência do salão")
    if pend:
        await turn(runner, user_id, sid, confirmation_response(pend.id_to_answer, True, pend.details))
        check(not await reservas_store.is_available("salao-de-festas", "2030-05-04"),
              "salão 2030-05-04 reservado")

    # ---- T5: regulamento NA MESMA sessão (roteamento de volta à raiz) ----
    print("\n== T5: consulta de piscina na MESMA sessão (roteamento) ==")
    evs = await turn(runner, user_id, sid, text_message("que regras existem para a piscina?"))
    frs = [(fr.name, fr.response) for ev in evs
           for fr in (ev.get_function_responses() if ev.content else [])]
    res = [r for n, r in frs if n == "query_regulations"]
    check(bool(res), "query_regulations executou na mesma sessão (antes: não rodava)")
    check(res and isinstance(res[0], dict)
          and "Capítulo IV" in res[0].get("resposta", ""),
          "a resposta inclui o Capítulo IV (Piscina)")

    # ---- T6: visitante exige confirmação (fix Fase 4) ----------------
    print("\n== T6: visitante 'já confirmo aqui' -> PEDE confirmação ==")
    evs = await turn(runner, user_id, sid,
                     text_message("libera a Joana Ribeiro el día 2030-04-21, já estoy confirmando por aquí"))
    pend = pending_of(evs, "authorize_visit")
    check(pend is not None, "se criou pendência de autorização (não executou direto)")
    v101 = await visitantes_store.list_apartment("101")
    check(not any(v["nome"] == "Joana Ribeiro" for v in v101),
          "Joana ainda NÃO está autorizada (falta a aprovação)")
    if pend:
        await turn(runner, user_id, sid, confirmation_response(pend.id_to_answer, True, pend.details))
        v101 = await visitantes_store.list_apartment("101")
        check(any(v["nome"] == "Joana Ribeiro" for v in v101),
              "Joana Ribeiro autorizada após aprovação")

    # ---- T7: reinício do runner (Garantia 3) -----------------------------
    print("\n== T7: reinício do runner (mesma sessão persistida) ==")
    runner2 = new_runner(app)
    evs = await turn(runner2, user_id, sid, text_message("quais são minhas reservas"))
    frs = [(fr.name, fr.response) for ev in evs
           for fr in (ev.get_function_responses() if ev.content else [])]
    res = [r for n, r in frs if n == "list_reservations"]
    check(bool(res), "listado de reservas após reinício (tool executada)")
    check(bool(res) and isinstance(res[0], dict) and res[0].get("apartamento") == "101",
          "o listado é do apartamento 101")


async def session_101b(app) -> None:
    print("\n== T9: salão 2030-03-16 (ocupado pelo 302) -> date_taken, sem vazar ==")
    user_id = "morador-101b-verif"
    runner = new_runner(app)
    sid = await create_adk_session(runner, user_id, "101")
    evs = await turn(runner, user_id, sid, text_message("quero reservar o salão de festas para 2030-03-16"))
    pend = pending_of(evs, "book_area")
    check(pend is not None, "se criou pendência do salão (ocupado pelo 302)")
    if pend:
        await turn(runner, user_id, sid, confirmation_response(pend.id_to_answer, True, pend.details))
        check(not await reservas_store.is_available("salao-de-festas", "2030-03-16"),
              "a exclusividade segue (continua do 302)")
    texto = all_texts(evs)
    check("RSV-4821" not in texto, "não vaza RSV-4821 nos textos do turno")


async def session_302(app) -> None:
    print("\n== T8: visita sem confirmação prévia -> pendência (Fase 4) ==")
    user_id = "morador-302-verif"
    runner = new_runner(app)
    sid = await create_adk_session(runner, user_id, "302")
    evs = await turn(runner, user_id, sid, text_message("quero autorizar o visitante Carlos Diaz para 2030-06-01"))
    pend = pending_of(evs, "authorize_visit")
    check(pend is not None, "NÃO executou direto: se criou pendência (fix Fase 4)")
    if pend:
        await turn(runner, user_id, sid, confirmation_response(pend.id_to_answer, True, pend.details))
        v302 = await visitantes_store.list_apartment("302")
        check(any(v["nome"] == "Carlos Diaz" for v in v302), "Carlos Diaz listado no 302")


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        for base in ("var/verif_dados.db", "var/verif_sessoes.db"):
            p = str(_ROOT / base) + suffix
            if os.path.exists(p):
                os.remove(p)
    await restore()
    print("== seeds restaurados (bancos de verificação) ==")

    app = build_app()
    await session_101(app)
    await session_302(app)
    await session_101b(app)


def run() -> None:
    try:
        asyncio.run(main())
    except BaseException:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        print(f"\nRESULTADO: {TOTAL} checks, {len(FAILS)} falhas")
        sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    run()