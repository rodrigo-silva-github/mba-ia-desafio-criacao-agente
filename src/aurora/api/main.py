"""API FastAPI do Residencial Aurora (contrato Fase 4).

Rotas (pt-BR, conforme o contrato do planejamento):
  POST /sessoes                       -> 201 + session_id
  POST /sessoes/{id}/mensagens        -> 200 + resposta + confirmacoes_pendentes
  POST /sessoes/{id}/confirmacoes     -> 200 (mesmo formato) | 409
  GET  /sessoes/{id}/eventos          -> 200 lista completa | 404
  GET  /apartamentos/{n}/reservas     -> verificação direta do store
  GET  /apartamentos/{n}/visitantes   -> idem

Roteamento entre turnos: os especialistas são `single_turn` com
`disallow_transfer_to_parent=True` (ver `agents/builder.py`), o que faz o
router do ADK voltar sempre ao principal nos turnos de texto e manter o
especialista que pediu a confirmação no turno de retomada. Ao fim de cada
turno resolvido a API anexa um evento sintético do principal (`anchor_root`),
garantindo que o último evento do turno seja do root; um turno que parou em
confirmação NÃO é ancorado.

A idempotência (D9c): a tabela `confirmations` é a fonte de verdade; duas
respostas simultâneas ao mesmo id: só a primeira vence (409 à segunda) e
nada é executado duas vezes.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import AliasChoices, BaseModel, Field

from .. import config
from ..agents.builder import (
    APP_NAME,
    anchor_root,
    create_adk_session,
    new_runner,
)
from ..agents.confirmations import confirmation_response, find_pending, text_message
from ..storage import confirmations_store, reservas_store, sessions_store, visitantes_store
from ..storage.db import init_db
from .serializers import event_to_dict

_runner = None


def get_runner():
    """Runner global (lazy): sessões persistidas em SQLite (Garantia 3)."""
    global _runner
    if _runner is None:
        _runner = new_runner()
    return _runner


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    get_runner()
    yield


app = FastAPI(title="Residencial Aurora", version="4.0.0", lifespan=lifespan)


# ------------------------------------------------------------------- schemas

class CreateSessionRequest(BaseModel):
    apartamento: str


class MessageRequest(BaseModel):
    # O contrato do enunciado usa `texto`; `mensagem` é aceito como apelido
    # para não quebrar clientes internos/scripts antigos.
    texto: str = Field(validation_alias=AliasChoices("texto", "mensagem"))


class ConfirmationRequest(BaseModel):
    id: str
    confirmado: bool


async def _pending_list(session_id: str):
    """Pendências no formato do contrato: {id, acao, detalhes}."""
    return [
        {"id": p["id"], "acao": p["action"], "detalhes": p["details"]}
        for p in await confirmations_store.list_pending(session_id)
    ]


def _last_text(events) -> str:
    """Último texto de modelo do turno (a 'resposta' que vê o morador)."""
    for event in reversed(events):
        if event.content and event.content.parts:
            for part in reversed(event.content.parts):
                if part.text:
                    return part.text
    return ""


async def _run_turn(session_row: dict, content, reanchor: bool = False) -> tuple[list, dict]:
    """Roda o turno ADK e sincroniza a tabela de pendências.

    Usa um Runner NOVO por turno (decisão D9d): cada rota abre sua própria
    conexão SQLite, o que faz com que escrituras concorrentes em sessões
    diferentes (passo 14 do avaliador: aprovações simultâneas) não se
    pisem, e simula fielmente o restart da API (Garantia 3: tudo se
    lê do banco de sessões).

    Retorna (eventos, resposta) onde `resposta` é '' se o turno parou em
    uma confirmação pendente (contrato da rota /mensagens).

    Ao fim de um turno SEM pendência a sessão é re-fixada no principal
    (`anchor_root`), para que a próxima mensagem do morador volte à raiz em
    vez de ficar presa no especialista do último domínio. Um turno que
    terminou pedindo confirmação NÃO é re-fixado: o último evento precisa
    continuar sendo o `adk_request_confirmation` do especialista para que a
    retomada chegue a ele (fix da pendência duplicada).

    `reanchor=True` (rota de mensagens): se a sessão tem pendência ativa, a
    raiz é re-fixada ANTES do turno. Sem isso, uma mensagem de texto enviada
    enquanto existe confirmação pendente cai no especialista que pediu a
    confirmação (o último evento é dele) e o pedido não chega ao domínio
    certo. Nada executa: a pendência segue intacta e só a rota
    /confirmacoes a resolve.
    """
    runner = new_runner()
    user_id = session_row["user_id"]
    session_id = session_row["session_id"]
    if reanchor and await confirmations_store.list_pending(session_id):
        await anchor_root(runner, user_id, session_id)
    events = []
    async for ev in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content
    ):
        events.append(ev)
    pending = find_pending(events)
    if pending is not None:
        await confirmations_store.add_or_ignore(
            session_id, pending.id_to_answer, pending.action, pending.details)
        return events, ""
    await anchor_root(runner, user_id, session_id)
    return events, _last_text(events)


async def _session_row(session_id: str) -> dict:
    row = await sessions_store.get(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return row


# --------------------------------------------------------------------- rotas

@app.post("/sessoes", status_code=201)
async def create_session(req: CreateSessionRequest):
    """Cria uma sessão nova com o apartamento fixo no estado (Garantia 2)."""
    apartment = req.apartamento.strip()
    if apartment not in config.load_apartments():
        raise HTTPException(status_code=400, detail="Apartamento desconhecido.")
    runner = get_runner()
    user_id = f"{apartment}-{uuid.uuid4().hex[:10]}"
    session_id = await create_adk_session(runner, user_id, apartment)
    await sessions_store.add(session_id, user_id, apartment)
    return {"session_id": session_id}


@app.post("/sessoes/{session_id}/mensagens")
async def send_message(session_id: str, req: MessageRequest):
    """Processa uma mensagem do morador na sessão."""
    row = await _session_row(session_id)
    events, resposta = await _run_turn(row, text_message(req.texto), reanchor=True)
    return {
        "resposta": resposta,
        "confirmacoes_pendentes": await _pending_list(session_id),
    }


@app.post("/sessoes/{session_id}/confirmacoes")
async def answer_confirmation(session_id: str, req: ConfirmationRequest):
    """Responde a uma confirmação pendente (200) ou 409 se inválida/repetida."""
    row = await _session_row(session_id)
    current = await confirmations_store.get(session_id, req.id)
    if current is None or current["status"] != "pending":
        raise HTTPException(status_code=409, detail="Confirmação desconhecida ou já respondida.")
    claimed = await confirmations_store.claim(session_id, req.id, req.confirmado)
    if not claimed:
        raise HTTPException(status_code=409, detail="Confirmação já respondida (resposta simultânea).")
    content = confirmation_response(req.id, req.confirmado, current["details"])
    events, resposta = await _run_turn(row, content)
    return {
        "resposta": resposta,
        "confirmacoes_pendentes": await _pending_list(session_id),
    }


@app.get("/sessoes/{session_id}/eventos")
async def get_events(session_id: str):
    """Lista completa de eventos da sessão, em ordem (para verificação)."""
    row = await _session_row(session_id)
    runner = get_runner()
    session = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=row["user_id"], session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    return [event_to_dict(ev) for ev in session.events]


@app.get("/apartamentos/{apartment}/reservas")
async def apartment_reservations(apartment: str):
    """Reservas ativas do apartamento (lista, formato do contrato)."""
    return await reservas_store.list_apartment(apartment)


@app.get("/apartamentos/{apartment}/visitantes")
async def apartment_visitors(apartment: str):
    """Visitantes autorizados do apartamento (lista, formato do contrato)."""
    return await visitantes_store.list_apartment(apartment)


@app.get("/health")
async def health():
    return {"status": "ok"}