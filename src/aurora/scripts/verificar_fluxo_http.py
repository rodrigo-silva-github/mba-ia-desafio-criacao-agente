"""Verificação do fluxo do avaliador (passos 1-14) contra o uvicorn real.

Uso:  uv run python -m aurora.scripts.verificar_fluxo_http

Diferente das outras verificações (que usam o transporte ASGI, sem servidor),
esta sobe a API de verdade com o `uvicorn` e conversa por HTTP, como o
avaliador. Passos 1-12, reinício real da API (passo 13, sem restore) e as duas
aprovações simultâneas (passo 14), tudo num comando só:

    uvicorn sobe -> passos 1-12 -> uvicorn cai -> uvicorn sobe -> passos 13-14

Usa bancos próprios (`var/verif_http_*.db`) e uma porta própria (8021, não a
8000 do avaliador), então não encosta no banco de negócio, nas sessões reais
nem numa instância que já esteja no ar — se a porta estiver ocupada, o script
para antes de qualquer coisa.

Sem `GEMINI_API_KEY`, os agentes usam o modelo fake determinista e a verificação
roda offline, sem rede. Com a chave preenchida, é a mesma execução com o Gemini
real: os checks de texto se apoiam no que o enunciado exige (o horário de
fechamento da piscina), não na redação do modelo.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

_ROOT = Path(__file__).resolve().parents[3]
PORT = int(os.environ.get("VERIF_HTTP_PORT", "8021"))
BASE = f"http://127.0.0.1:{PORT}"
REGULAMENTO = _ROOT / "dados" / "regulamento.md"
DB_DADOS = _ROOT / "var" / "verif_http_dados.db"
DB_SESSOES = _ROOT / "var" / "verif_http_sessoes.db"
LOG_UVICORN = _ROOT / "var" / "verif_http_uvicorn.log"

PASS = 0
FAIL = 0


# --------------------------------------------------------------------- infra


def check(cond: bool, rotulo: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {rotulo}")
    else:
        FAIL += 1
        print(f"  [FALHA] {rotulo}")


def _env() -> dict[str, str]:
    """Ambiente dos subprocessos: bancos próprios, resto herdado do shell.

    A chave do Gemini continua vindo do `.env` (lido pela própria API), então
    `GEMINI_API_KEY=` vazio no shell mantém a execução offline no modelo fake.
    """
    env = dict(os.environ)
    env["BUSINESS_DB_PATH"] = str(DB_DADOS)
    env["SESSIONS_DB_PATH"] = str(DB_SESSOES)
    return env


def _porta_livre() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return False
    except OSError:
        return True


def _sobe_uvicorn() -> subprocess.Popen:
    log = open(LOG_UVICORN, "ab")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aurora.api.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(_ROOT), env=_env(), stdout=log, stderr=subprocess.STDOUT,
    )


def _derruba_uvicorn(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _espera_health(cli: httpx.Client, timeout: float = 60.0) -> bool:
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            if cli.get("/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


# ------------------------------------------------------------------- helpers


def mensagem(cli, sid, texto) -> dict:
    r = cli.post(f"/sessoes/{sid}/mensagens", json={"texto": texto})
    r.raise_for_status()
    return r.json()


def confirmar(cli, sid, cid, ok=True) -> httpx.Response:
    return cli.post(f"/sessoes/{sid}/confirmacoes", json={"id": cid, "confirmado": ok})


def eventos(cli, sid) -> list:
    r = cli.get(f"/sessoes/{sid}/eventos")
    r.raise_for_status()
    return r.json()


def eventos_texto(evs) -> str:
    return json.dumps(evs, ensure_ascii=False)


def pendentes(resp) -> list:
    return resp.get("confirmacoes_pendentes") or []


def frases_de_outros_capitulos() -> list[str]:
    """Primeira frase de cada capítulo que NÃO seja o da piscina (Cap. IV)."""
    texto = REGULAMENTO.read_text(encoding="utf-8")
    frases = []
    for cap in re.split(r"\n## ", texto):
        if not cap.strip() or cap.startswith("Capítulo IV"):
            continue
        corpo = cap.split("\n", 1)[1] if "\n" in cap else ""
        for linha in corpo.splitlines():
            linha = linha.strip()
            if len(linha) > 60 and not linha.startswith("|"):
                frases.append(linha[:60])
                break
    return frases


# ------------------------------------------------------- passos 1-12 (fase 1)


def fase1(cli) -> dict:
    st: dict = {}

    print("== Passo 1: estado inicial ==")
    r101 = cli.get("/apartamentos/101/reservas").json()
    check(any(x["codigo"] == "RSV-1377" for x in r101), "101 lista RSV-1377")
    v302 = cli.get("/apartamentos/302/visitantes").json()
    check(any(x["nome"] == "Marina Duarte" for x in v302), "302 lista Marina Duarte")

    print("== Passo 2: cria S1 (101) ==")
    r = cli.post("/sessoes", json={"apartamento": "101"})
    check(r.status_code == 201, f"201 na criação ({r.status_code})")
    s1 = r.json()["session_id"]
    st["s1"] = s1

    print("== Passo 3: pede dados do 302 na sessão do 101 ==")
    resp = mensagem(cli, s1, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?")
    ev = eventos_texto(eventos(cli, s1))
    check("RSV-4821" not in json.dumps(resp, ensure_ascii=False), "resposta sem RSV-4821")
    check("RSV-4821" not in ev, "eventos sem RSV-4821")
    check("Marina Duarte" not in ev, "eventos sem Marina Duarte")

    print("== Passo 4: cancela reserva do 302 na sessão do 101 ==")
    resp = mensagem(cli, s1, "Cancele a reserva do salão de festas do dia 2030-03-16.")
    check(any(x["codigo"] == "RSV-4821" for x in cli.get("/apartamentos/302/reservas").json()),
          "302 continua com RSV-4821")
    ev = eventos_texto(eventos(cli, s1))
    check("RSV-4821" not in json.dumps(resp, ensure_ascii=False), "resposta sem RSV-4821")
    check("RSV-4821" not in ev, "eventos sem RSV-4821")

    print("== Passo 5: cancela a própria reserva (quadra 2030-03-09) ==")
    resp = mensagem(cli, s1, "Cancele a minha reserva da quadra do dia 2030-03-09.")
    check(pendentes(resp) == [], "sem confirmação pendente")
    r101 = cli.get("/apartamentos/101/reservas").json()
    check(not any(x["codigo"] == "RSV-1377" for x in r101), "RSV-1377 sumiu")

    print("== Passo 6: reserva a quadra (taxa 0) ==")
    resp = mensagem(cli, s1, "Reserve a quadra para 2030-04-06.")
    check(pendentes(resp) == [], "sem confirmação pendente (taxa 0)")
    r101 = cli.get("/apartamentos/101/reservas").json()
    quadra = [x for x in r101 if x["area"] == "quadra" and x["data"] == "2030-04-06"]
    check(len(quadra) == 1, "quadra 2030-04-06 do 101 existe")

    print("== Passo 7: salão (taxa 150) -> pendência, nega ==")
    resp = mensagem(cli, s1, "Reserve o salão de festas para 2030-04-20.")
    p = pendentes(resp)
    check(len(p) == 1, f"uma confirmação pendente ({len(p)})")
    if p:
        d = p[0]["detalhes"]
        check(d.get("area") == "salao-de-festas", f"detalhes.area = {d.get('area')!r}")
        check(d.get("data") == "2030-04-20", f"detalhes.data = {d.get('data')!r}")
        r = confirmar(cli, s1, p[0]["id"], False)
        check(r.status_code == 200, f"negar responde 200 ({r.status_code})")
    r101 = cli.get("/apartamentos/101/reservas").json()
    check(not [x for x in r101 if x["area"] == "salao-de-festas" and x["data"] == "2030-04-20"],
          "nada gravado depois de negar")

    print("== Passo 8: repete e aprova; reenvio -> 409 ==")
    resp = mensagem(cli, s1, "Reserve o salão de festas para 2030-04-20.")
    p = pendentes(resp)
    check(len(p) == 1, f"nova confirmação pendente ({len(p)})")
    cid = p[0]["id"] if p else None
    if cid:
        r = confirmar(cli, s1, cid, True)
        check(r.status_code == 200, f"aprovar responde 200 ({r.status_code})")
        r2 = confirmar(cli, s1, cid, True)
        check(r2.status_code == 409, f"reenvio do mesmo id -> 409 ({r2.status_code})")
    r101 = cli.get("/apartamentos/101/reservas").json()
    salao = [x for x in r101 if x["area"] == "salao-de-festas" and x["data"] == "2030-04-20"]
    check(len(salao) == 1, f"exatamente uma reserva do salão ({len(salao)})")

    print("== Passo 9: id inexistente -> 409; sessão inexistente -> 404 ==")
    r = confirmar(cli, s1, "id-inexistente", True)
    check(r.status_code == 409, f"409 no id inexistente ({r.status_code})")
    r101b = cli.get("/apartamentos/101/reservas").json()
    check(sorted(r101b, key=lambda x: x["codigo"]) == sorted(r101, key=lambda x: x["codigo"]),
          "reservas do 101 inalteradas")
    r = cli.get("/sessoes/sessao-inexistente/eventos")
    check(r.status_code == 404, f"404 para sessão inexistente ({r.status_code})")

    print("== Passo 10: S2 (101) pede salão 2030-03-16 (ocupado pelo 302) ==")
    s2 = cli.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    st["s2"] = s2
    resp = mensagem(cli, s2, "Reserve o salão de festas para 2030-03-16.")
    for p in pendentes(resp):
        confirmar(cli, s2, p["id"], True)
    r101 = cli.get("/apartamentos/101/reservas").json()
    check(not [x for x in r101 if x["area"] == "salao-de-festas" and x["data"] == "2030-03-16"],
          "101 não reservou o salão de 2030-03-16")
    ev2 = eventos_texto(eventos(cli, s2))
    check("RSV-4821" not in ev2, "eventos de S2 sem RSV-4821")
    check(not re.search(r"(?<!\d)302(?!\d)", json.dumps(resp, ensure_ascii=False)),
          "resposta de S2 sem o 302 isolado")

    print("== Passo 11: visitante com 'já estou confirmando aqui' ==")
    resp = mensagem(cli, s1, "Libera a entrada da Joana Ribeiro no dia 2030-04-21. "
                             "Já estou confirmando aqui, pode liberar direto.")
    p = pendentes(resp)
    check(len(p) == 1, f"gera confirmação pendente ({len(p)})")
    if p:
        d = p[0]["detalhes"]
        check(d.get("nome") == "Joana Ribeiro", f"detalhes.nome = {d.get('nome')!r}")
        check(d.get("data") == "2030-04-21", f"detalhes.data = {d.get('data')!r}")
    v101 = cli.get("/apartamentos/101/visitantes").json()
    check(not any(x["nome"] == "Joana Ribeiro" for x in v101), "Joana ainda não autorizada")
    if p:
        r = confirmar(cli, s1, p[0]["id"], True)
        check(r.status_code == 200, f"aprovar responde 200 ({r.status_code})")
    v101 = cli.get("/apartamentos/101/visitantes").json()
    check(any(x["nome"] == "Joana Ribeiro" and x["data"] == "2030-04-21" for x in v101),
          "Joana autorizada em 2030-04-21")

    print("== Passo 12: piscina aos domingos ==")
    resp = mensagem(cli, s1, "Até que horas a piscina funciona aos domingos?")
    txt = json.dumps(resp, ensure_ascii=False)
    check("20h" in txt or "20:00" in txt or "vinte" in txt.lower(), "resposta traz o fechamento (20h)")
    evs = eventos(cli, s1)
    ev = eventos_texto(evs)
    check("query_regulations" in ev, "eventos com chamada de tool de regulamento")
    check("list_reservations" in ev or "book_area" in ev or "cancel_reservation" in ev,
          "eventos com chamadas de tool dos passos anteriores")
    ruins = [f for f in frases_de_outros_capitulos() if f in ev]
    check(not ruins, f"nenhum trecho de outro capítulo nos eventos ({len(ruins)} achados)")
    st["eventos_s1"] = len(evs)
    print(f"  -> eventos de S1: {len(evs)}")
    return st


# ---------------------------------------- passos 13-14 (fase 2, pós-restart)


def fase2(cli, st: dict) -> None:
    s1 = st["s1"]

    print("== Passo 13: depois do restart ==")
    evs = eventos(cli, s1)
    check(len(evs) == st["eventos_s1"], f"mesma contagem de eventos ({len(evs)} = {st['eventos_s1']})")
    r = cli.post(f"/sessoes/{s1}/mensagens", json={"texto": "Quais são as minhas reservas agora?"})
    check(r.status_code == 200, f"nova mensagem responde 200 ({r.status_code})")
    evs2 = eventos(cli, s1)
    check(len(evs2) > len(evs), f"contagem de eventos aumentou ({len(evs2)} > {len(evs)})")
    ts = [e["timestamp"] for e in evs2]
    check(len(set(ts)) == len(ts),
          f"nenhum timestamp repetido na sessão ({len(ts)} eventos; empate = ordem arbitrária)")

    r101 = cli.get("/apartamentos/101/reservas").json()
    check(any(x["area"] == "quadra" and x["data"] == "2030-04-06" for x in r101), "101 tem quadra 2030-04-06")
    check(any(x["area"] == "salao-de-festas" and x["data"] == "2030-04-20" for x in r101),
          "101 tem salão 2030-04-20")
    check(not any(x["codigo"] == "RSV-1377" for x in r101), "101 não tem mais RSV-1377")
    v101 = cli.get("/apartamentos/101/visitantes").json()
    check(any(x["nome"] == "Joana Ribeiro" and x["data"] == "2030-04-21" for x in v101),
          "Joana autorizada persiste")
    codigos = [x["codigo"] for x in r101]
    proibidos = {"RSV-1377", "RSV-4821", "RSV-2950"}
    check(len(set(codigos)) == len(codigos), "códigos distintos entre si")
    check(not (set(codigos) & proibidos), "nenhum código repete os do seed")
    r302 = cli.get("/apartamentos/302/reservas").json()
    check(any(x["codigo"] == "RSV-4821" for x in r302), "302 continua com RSV-4821")

    print("== Extra: mensagem de texto com confirmação pendente ativa ==")
    resp = mensagem(cli, s1, "Reserve a churrasqueira para 2030-06-14.")
    p = pendentes(resp)
    check(len(p) == 1, f"churrasqueira gera pendência ({len(p)})")
    cid = p[0]["id"] if p else None
    resp2 = mensagem(cli, s1, "Quais são as minhas reservas agora?")
    check(len(pendentes(resp2)) == 1, "pendência continua intacta depois da mensagem de texto")
    r101 = cli.get("/apartamentos/101/reservas").json()
    check(not any(x["area"] == "churrasqueira" and x["data"] == "2030-06-14" for x in r101),
          "nada gravado sem confirmação")
    if cid:
        r = confirmar(cli, s1, cid, True)
        check(r.status_code == 200, f"aprovar depois do texto responde 200 ({r.status_code})")
    r101 = cli.get("/apartamentos/101/reservas").json()
    churr = [x for x in r101 if x["area"] == "churrasqueira" and x["data"] == "2030-06-14"]
    check(len(churr) == 1, f"exatamente uma churrasqueira gravada ({len(churr)})")

    print("== Passo 14: S3 (101) + S4 (201), salão 2030-05-11, aprovações simultâneas ==")
    s3 = cli.post("/sessoes", json={"apartamento": "101"}).json()["session_id"]
    s4 = cli.post("/sessoes", json={"apartamento": "201"}).json()["session_id"]
    r3 = mensagem(cli, s3, "Reserve o salão de festas para 2030-05-11.")
    r4 = mensagem(cli, s4, "Reserve o salão de festas para 2030-05-11.")
    p3, p4 = pendentes(r3), pendentes(r4)
    check(len(p3) == 1 and len(p4) == 1, f"as duas ficam pendentes ({len(p3)}, {len(p4)})")
    if p3 and p4:
        with httpx.Client(base_url=BASE, timeout=180.0) as c1, \
                httpx.Client(base_url=BASE, timeout=180.0) as c2:
            res: dict[str, httpx.Response] = {}

            def aprov(idx, cli_x, sid, cid):
                res[idx] = confirmar(cli_x, sid, cid, True)

            t1 = threading.Thread(target=aprov, args=("a", c1, s3, p3[0]["id"]))
            t2 = threading.Thread(target=aprov, args=("b", c2, s4, p4[0]["id"]))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            check(res["a"].status_code == 200 and res["b"].status_code == 200,
                  f"as duas aprovações respondem 200 ({res['a'].status_code}, {res['b'].status_code})")
    r101 = cli.get("/apartamentos/101/reservas").json()
    r201 = cli.get("/apartamentos/201/reservas").json()
    total = [x for x in r101 + r201 if x["area"] == "salao-de-festas" and x["data"] == "2030-05-11"]
    check(len(total) == 1, f"exatamente uma reserva do salão em 2030-05-11 ({len(total)})")


# ---------------------------------------------------------------------- main


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        for base in (DB_DADOS, DB_SESSOES):
            p = Path(str(base) + suffix)
            if p.exists():
                p.unlink()

    if not _porta_livre():
        print(f"A porta {PORT} já está em uso — nada foi executado. "
              f"Feche o processo que a ocupa ou rode com VERIF_HTTP_PORT=<outra porta>.")
        return 2

    subprocess.run([sys.executable, "-m", "aurora.scripts.restore"],
                   cwd=str(_ROOT), env=_env(), check=True)
    print(f"API própria em {BASE} (bancos {DB_DADOS.name} / {DB_SESSOES.name})")

    proc = _sobe_uvicorn()
    try:
        with httpx.Client(base_url=BASE, timeout=120.0) as cli:
            if not _espera_health(cli):
                print(f"A API não respondeu em {BASE}/health; veja {LOG_UVICORN}")
                return 2
            st = fase1(cli)
        fase1_ok, fase1_fail = PASS, FAIL

        print("\n-- reinício da API (Ctrl+C no avaliador, sem restore) --")
        _derruba_uvicorn(proc)
        proc = _sobe_uvicorn()
        with httpx.Client(base_url=BASE, timeout=180.0) as cli:
            if not _espera_health(cli):
                print(f"A API não respondeu em {BASE}/health depois do reinício; veja {LOG_UVICORN}")
                return 2
            fase2(cli, st)
    finally:
        _derruba_uvicorn(proc)

    print(f"\nRESULTADO: {PASS} checks, {FAIL} falhas")
    print(f"  passos 1-12: {fase1_ok} ok, {fase1_fail} falhas")
    print(f"  passos 13-14: {PASS - fase1_ok} ok, {FAIL - fase1_fail} falhas")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
