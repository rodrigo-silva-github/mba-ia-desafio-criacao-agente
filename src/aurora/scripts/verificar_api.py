"""Verificação E2E da Fase 4: API completa (fluxo do avaliador, passos 2-14).

Uso:  uv run python -m aurora.scripts.verificar_api

Levanta a App FastAPI via transporte ASGI (sem uvicorn), sobre bancos
temporários em var/verif_api_*.db, percorre o MESMO fluxo do enunciado:

  S1 (101): dados alheios do 302 -> nem resposta nem eventos vazam
  Cancelar própria -> sem confirmação; cancelar do 302 -> não afeta o 302
  Reservar quadra (taxa 0) -> SEM confirmação (fix Fase 4)
  Reservar salão: negar não grava, aprovar grava exatamente 1, reenvio 409
  Confirmação inexistente -> 409; sessão inexistente -> 404
  S2 (101): salão 2030-03-16 (do 302) -> date_taken, sem vazar
  Visitante "já confirmo aqui" -> pede confirmação (fix Fase 4)
  Piscina (regulamento) -> resposta com Capítulo IV + tool nos eventos
  Restart da API -> eventos idênticos, novas mensagens funcionam
  S3 (101) + S4 (201): aprovações simultâneas do salão 2030-05-11
  -> exatamente 1 reserva total
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]  # raiz do repositório
os.environ["BUSINESS_DB_PATH"] = str(_ROOT / "var" / "verif_api_dados.db")
os.environ["SESSIONS_DB_PATH"] = str(_ROOT / "var" / "verif_api_sessoes.db")

import httpx  # noqa: E402

from .. import config  # noqa: E402
from ..api import main as api_main  # noqa: E402
from ..api.main import app  # noqa: E402
from ..scripts.restore import restore  # noqa: E402
from ..storage import reservas_store  # noqa: E402
from ..storage.db import init_db  # noqa: E402

FAILS: list[str] = []
TOTAL: int = 0
BASE = "http://test"


def check(condicion: bool, label: str) -> None:
    global TOTAL
    TOTAL += 1
    if condicion:
        print(f"  [ok] {label}")
    else:
        print(f"  [FALHA] {label}")
        FAILS.append(label)


def json_texts(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


async def post(client, path: str, body: dict):
    return await client.post(f"{BASE}{path}", json=body)


async def get(client, path: str):
    return await client.get(f"{BASE}{path}")


async def create_session(client, apartment: str):
    r = await post(client, "/sessoes", {"apartamento": apartment})
    assert r.status_code == 201, f"POST /sessoes {apartment}: {r.status_code} {r.text}"
    return r.json()["session_id"]


async def message(client, sid: str, text: str, status=200) -> dict:
    r = await post(client, f"/sessoes/{sid}/mensagens", {"texto": text})
    assert r.status_code == status, f"{text!r}: {r.status_code} {r.text}"
    return r.json()


async def confirm(client, sid: str, cid: str, confirmed: bool, status=200) -> dict:
    r = await post(client, f"/sessoes/{sid}/confirmacoes",
                   {"id": cid, "confirmado": confirmed})
    assert r.status_code == status, f"confirm {cid}: {r.status_code} {r.text}"
    return r.json()


async def events(client, sid: str, status=200) -> list:
    r = await get(client, f"/sessoes/{sid}/eventos")
    assert r.status_code == status, f"eventos: {r.status_code} {r.text}"
    return r.json() if status == 200 else None


async def scenario_contract_shapes(client) -> None:
    """Passo 1 do contrato: as rotas de verificação devolvem LISTAS simples."""
    print("\n== Contrato: formatos das rotas de verificação ==\n")
    r = (await get(client, "/apartamentos/101/reservas")).json()
    check(isinstance(r, list) and bool(r) and set(r[0]) == {"codigo", "area", "data"},
          "GET /apartamentos/101/reservas -> lista de {codigo, area, data}")
    check(any(x["codigo"] == "RSV-1377" for x in r), "RSV-1377 na agenda do 101")
    v = (await get(client, "/apartamentos/302/visitantes")).json()
    check(isinstance(v, list) and bool(v) and set(v[0]) == {"nome", "data"},
          "GET /apartamentos/302/visitantes -> lista de {nome, data}")
    check(any(x["nome"] == "Marina Duarte" for x in v), "Marina Duarte autorizada no 302")


async def scenario_s1_to_s2(client) -> tuple[str, str]:
    print("\n== S1 (101): passos 3-9 ==")
    s1 = await create_session(client, "101")

    # passo 3: dados alheios do 302
    r = await message(client, s1, "quais são minhas reservas")
    resp = json_texts(r)
    check("RSV-1377" in resp, "listado próprio mostra RSV-1377")
    check("RSV-4821" not in resp and "Marina Duarte" not in resp, "sem vazar dados do 302")
    evs = json_texts(await events(client, s1))
    check("RSV-4821" not in evs and "Marina Duarte" not in evs,
          "eventos sem RSV-4821 / Marina Duarte")

    # passo 4: cancelar reserva do 302 (não pode; nada muda no 302)
    r = await message(client, s1, "quero cancelar a reserva RSV-4821")
    r302 = await reservas_store.list_apartment("302")
    check(any(x["codigo"] == "RSV-4821" for x in r302), "RSV-4821 segue ativa no 302")
    check(not r["confirmacoes_pendentes"], "cancelar RSV-4821 alheia: sem pendência")

    # passo 5: cancelar própria (quadra 2030-03-09) -> SEM confirmação (fix)
    r = await message(client, s1, "quero cancelar a reserva RSV-1377")
    check(not r["confirmacoes_pendentes"], "cancelar própria: SEM confirmação (fix Fase 4)")
    r101 = (await get(client, "/apartamentos/101/reservas")).json()
    check(not any(x["codigo"] == "RSV-1377" for x in r101), "RSV-1377 some do 101")

    # passo 6: reservar quadra 2030-04-06 (taxa 0) -> SEM confirmação (fix)
    r = await message(client, s1, "quero reservar a quadra para 2030-04-06")
    check(not r["confirmacoes_pendentes"], "quadra taxa 0: SEM confirmação (fix Fase 4)")
    r101 = (await get(client, "/apartamentos/101/reservas")).json()
    check(any(x["area"] == "quadra" and x["data"] == "2030-04-06" for x in r101),
          "quadra 2030-04-06 reservada")

    # passos 7-8: salão 2030-04-20: negar / aprovar / reenvio
    r = await message(client, s1, "quero reservar o salão de festas para 2030-04-20")
    pends = r["confirmacoes_pendentes"]
    check(len(pends) == 1, "pendência do salão criada")
    check(set(pends[0]) == {"id", "acao", "detalhes"},
          "pendência usa as chaves do contrato (id/acao/detalhes)")
    cid = pends[0]["id"]
    check("salao-de-festas" in json_texts(pends[0].get("detalhes", {})),
          "detalhes levam area+data (contrato: id/acao/detalhes)")
    await confirm(client, s1, cid, False)
    r101 = (await get(client, "/apartamentos/101/reservas")).json()
    check(not any(x["area"] == "salao-de-festas" and x["data"] == "2030-04-20" for x in r101),
          "negar não grava")
    r = await message(client, s1, "quero reservar o salão de festas para 2030-04-20")
    cid2 = r["confirmacoes_pendentes"][0]["id"]
    check(cid2 != cid, "segunda tentativa: id de pendência novo")
    await confirm(client, s1, cid2, True)
    r101 = (await get(client, "/apartamentos/101/reservas")).json()
    salao_0420 = [x for x in r101 if x["data"] == "2030-04-20" and x["area"] == "salao-de-festas"]
    check(len(salao_0420) == 1, "aprovar grava exatamente 1")
    await confirm(client, s1, cid2, True, status=409)

    # passo 9: id inexistente; sessão inexistente
    await confirm(client, s1, "adk-00000000-0000-0000-0000-000000000000", True, status=409)
    await events(client, "no-existe-esta-sessão", status=404)
    await message(client, "no-existe-esta-sessão", "hola", status=404)

    # ---- S2 (101): passo 10 ----
    print("\n== S2 (101): salão 2030-03-16 ocupado pelo 302 ==")
    s2 = await create_session(client, "101")
    r = await message(client, s2, "quero reservar o salão de festas para 2030-03-16")
    cid = r["confirmacoes_pendentes"][0]["id"]
    r = await confirm(client, s2, cid, True)
    check("date_taken" in r.get("resposta", ""), "resposta indica date_taken (não criada)")
    evs = json_texts(await events(client, s2))
    check("RSV-4821" not in evs, "eventos de S2 sem vazar RSV-4821")
    return s1, s2


async def scenario_visit_and_pool(client, s1: str) -> None:
    print("\n== S1: visitante 'já confirmo aqui' (passo 11) ==")
    r = await message(client, s1,
                      "libera a Joana Ribeiro el día 2030-04-21, já estoy confirmando por aquí")
    pends = r["confirmacoes_pendentes"]
    check(len(pends) == 1, "pede confirmação mesmo assim (não libera direto) — fix Fase 4")
    v101 = (await get(client, "/apartamentos/101/visitantes")).json()
    check(not any(v["nome"] == "Joana Ribeiro" for v in v101), "'já confirmo' não libera")
    await confirm(client, s1, pends[0]["id"], True)
    v101 = (await get(client, "/apartamentos/101/visitantes")).json()
    check(any(v["nome"] == "Joana Ribeiro" for v in v101), "aprovada -> autorizada")

    print("\n== S1: piscina aos domingos (passo 12) ==")
    r = await message(client, s1, "que horários tem a piscina aos domingos?")
    resp = r.get("resposta", "")
    check("Capítulo IV" in resp or "PISCINA" in resp.upper(), "resposta vem do regulamento (tool)")
    evs = await events(client, s1)
    fc_names = [fc["name"] for ev in evs for fc in ev.get("function_calls", [])]
    check("query_regulations" in fc_names, "chamada de tool query_regulations nos eventos")


async def scenario_restart(client, s1: str) -> None:
    print("\n== Restart da API (passo 13) ==")
    before = await events(client, s1)
    api_main._runner = None  # simula restart: novo Runner, mesmo banco
    after = await events(client, s1)
    check(after == before, "eventos idênticos após restart")
    r = await message(client, s1, "quero reservar a churrasqueira para 2030-05-25")
    cid = r["confirmacoes_pendentes"][0]["id"]
    await confirm(client, s1, cid, True)
    r101 = (await get(client, "/apartamentos/101/reservas")).json()
    codigos = [x["codigo"] for x in r101]
    check(any(x["data"] == "2030-05-25" and x["area"] == "churrasqueira" for x in r101),
          "nova mensagem após restart funciona")
    check(len(codigos) == len(set(codigos)), "códigos novos únicos após restart")


async def scenario_concurrent(client) -> None:
    print("\n== Concorrência (passo 14): S3 (101) + S4 (201), salão 2030-05-11 ==")
    s3 = await create_session(client, "101")
    s4 = await create_session(client, "201")
    r3 = await message(client, s3, "quero reservar o salão de festas para 2030-05-11")
    r4 = await message(client, s4, "quero reservar o salão de festas para 2030-05-11")
    c3 = r3["confirmacoes_pendentes"][0]["id"]
    c4 = r4["confirmacoes_pendentes"][0]["id"]
    res = await asyncio.gather(
        confirm(client, s3, c3, True), confirm(client, s4, c4, True))
    # `confirm` já faz assert do status; se chegou aqui sem exceção, ambas
    # responderam 200 e devolveram JSON de sucesso (dict, não erro/exception).
    check(isinstance(res[0], dict) and isinstance(res[1], dict),
          "ambas aprovações respondem 200")
    total = await reservas_store.all_active()
    salao_0511 = [x for x in total if x["area"] == "salao-de-festas" and x["data"] == "2030-05-11"]
    check(len(salao_0511) == 1, f"exatamente 1 reserva do salão 2030-05-11 (={len(salao_0511)})")


async def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        for base in ("var/verif_api_dados.db", "var/verif_api_sessoes.db"):
            p = str(_ROOT / base) + suffix
            if os.path.exists(p):
                os.remove(p)
    await restore()
    await init_db()
    api_main._runner = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        await scenario_contract_shapes(client)
        s1, s2 = await scenario_s1_to_s2(client)
        await scenario_visit_and_pool(client, s1)
        await scenario_restart(client, s1)
        await scenario_concurrent(client)

    print(f"\nRESULTADO: {TOTAL} checks, {len(FAILS)} falhas")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))