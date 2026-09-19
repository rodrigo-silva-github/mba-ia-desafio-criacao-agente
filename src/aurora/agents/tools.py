"""Tools de negócio do Residencial Aurora.

Contratos de desenho (ver PLANEJAMENTO.md, seção 4.3):
- O apartamento NUNCA chega como argumento do modelo: é lido do estado da
  sessão (`tool_context.state['apartamento']`); a API o fixa na criação
  (Garantia 2). Nenhuma tool aceita um apartamento escolhido pelo modelo.

Regras de confirmação (Fase 4, decididas pelo fluxo do avaliador):
- `book_area` pede confirmação AVANÇADA só quando a área gera cobrança
  (taxa > 0, regra 2 do enunciado: passos 7-8). Com taxa 0 grava direto,
  sem pendência (passo 6).
- `authorize_visit` SEMPRE pede confirmação (libera acesso, regra 3),
  mesmo quando o morador escreve "já estou confirmando por aqui" (passo 11).
- `cancel_reservation` NÃO pede confirmação (regra 4: cancelamento próprio).
- Leituras (listar, disponibilidade, regulamento) NÃO exigem confirmação.

O payload da confirmação contém os detalhes exatos que o morador aprova; a
gravação usa os valores do payload (não os args do modelo). No turno
confirmado o framework entrega `tool_context.tool_confirmation`.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools import ToolContext

from ..config import load_areas
from ..storage import reservas_store, visitantes_store
from . import regulations

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

AREAS = load_areas()  # id -> taxa (0 = sem taxa, não gera cobrança)


def _apartment(tool_context: ToolContext) -> str:
    """Apartamento da sessão; o modelo nunca o escolhe (vem do contexto)."""
    return str(tool_context.state.get("apartamento") or "")


def _valid_date(date: str) -> bool:
    return bool(DATE_RE.match(date or ""))


def _invalid(area: str, date: str) -> str | None:
    if area not in AREAS:
        return "área desconhecida"
    if not _valid_date(date):
        return "data inválida (AAAA-MM-DD)"
    return None


# ---------------------------------------------------------------- reservas

async def book_area(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Reserva uma área comum do apartamento da sessão.

    Pede confirmação só se a área tem taxa > 0 (regra 2); com taxa 0 grava
    direto. A gravação usa os valores do payload aprovado.
    """
    apartment = _apartment(tool_context)
    tc = tool_context.tool_confirmation
    if tc is None:
        problema = _invalid(area, data)
        if problema:
            return {"status": "invalid_requirements", "message": f"Requisitos inválidos: {problema}."}
        if AREAS[area] <= 0:  # sem taxa -> grava direto (passo 6 do fluxo)
            return await _do_book(apartment, area, data)
        tool_context.request_confirmation(
            hint=f"Reservar {area} para {data}? Esta reserva gera uma cobrança para o {apartment}.",
            payload={"area": area, "data": data, "apartamento": apartment},
        )
        return {"status": "awaiting_confirmation", "message": "Confirmação pendente."}
    if not tc.confirmed:
        return {"status": "refused_by_resident", "message": "Reserva descartada."}
    approved = tc.payload or {}
    area = approved.get("area") or area
    data = approved.get("data") or data
    apartment = approved.get("apartamento") or apartment
    if _invalid(area, data):
        return {"status": "invalid_requirements",
                "message": "Os dados aprovados não passam validação (área ou data)."}
    return await _do_book(apartment, area, data)


async def _do_book(apartment: str, area: str, data: str) -> dict[str, Any]:
    r = await reservas_store.book(apartment, area, data)
    if not r.ok:
        return {"status": "date_taken",
                "message": f"A {area} no {data} já está reservada por outro apartamento."}
    return {"status": "ok", "codigo": r.reservation.codigo, "area": r.reservation.area,
            "data": r.reservation.data, "apartamento": apartment}


async def cancel_reservation(codigo: str, tool_context: ToolContext) -> dict[str, Any]:
    """Cancela uma reserva própria: regra 4, sem confirmação.

    O cancelamento NÃO gera cobrança nem libera acesso -> NÃO pede
    confirmação (passo 5 do fluxo).
    """
    apartment = _apartment(tool_context)
    ok = await reservas_store.cancel(apartment, codigo)
    if not ok:
        return {"status": "not_found",
                "message": "Não encontrei uma reserva com este código para o seu apartamento."}
    return {"status": "ok", "codigo": codigo, "message": "Reserva cancelada."}


async def list_reservations(tool_context: ToolContext) -> dict[str, Any]:
    """Lista as reservas ativas do apartamento da sessão."""
    apartment = _apartment(tool_context)
    return {"apartamento": apartment,
            "reservas": await reservas_store.list_apartment(apartment)}


async def check_availability(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Consulta disponibilidade; retorna SOMENTE livre/ocupada (Garantia 2)."""
    del tool_context
    available = await reservas_store.is_available(area, data)
    return {"area": area, "data": data, "available": available}


# -------------------------------------------------------------- visitantes

async def authorize_visit(nome: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
    """Autoriza a entrada de um visitante.

    Libera acesso (regra 3) -> SEMPRE pede confirmação, ainda que o morador
    diga "já estou confirmando por aqui" (passo 11 do fluxo).
    """
    apartment = _apartment(tool_context)
    tc = tool_context.tool_confirmation
    if tc is None:
        if not (_valid_date(data) and nome and nome.strip()):
            return {"status": "invalid_requirements", "message": "Falta o nome ou a data (AAAA-MM-DD)."}
        tool_context.request_confirmation(
            hint=f"Autorizar a entrada de {nome.strip()} no dia {data} para o apartamento {apartment}?",
            payload={"nome": nome.strip(), "data": data, "apartamento": apartment},
        )
        return {"status": "awaiting_confirmation", "message": "Confirmação pendente."}
    if not tc.confirmed:
        return {"status": "refused_by_resident", "message": "Autorização descartada."}
    approved = tc.payload or {}
    nome = (approved.get("nome") or nome).strip()
    data = approved.get("data") or data
    apartment = approved.get("apartamento") or apartment
    if not (_valid_date(data) and nome):
        return {"status": "invalid_requirements", "message": "Os dados aprovados não passam validação."}
    v = await visitantes_store.authorize(apartment, nome, data)
    return {"status": "ok", "apartamento": apartment, "nome": v["nome"], "data": v["data"]}


async def list_visitors(tool_context: ToolContext) -> dict[str, Any]:
    """Lista os visitantes autorizados do apartamento da sessão."""
    apartment = _apartment(tool_context)
    return {"apartamento": apartment,
            "visitantes": await visitantes_store.list_apartment(apartment)}


# ------------------------------------------------------------- regulamento

async def query_regulations(consulta: str, tool_context: ToolContext) -> dict[str, Any]:
    """Busca regras relevantes do regulamento interno (Garantia 4)."""
    del tool_context
    return {"consulta": consulta, "resposta": regulations.search(consulta)}


# ------------------------------------------------------------------ tools

TOOLS_RESERVAS = [
    book_area,
    cancel_reservation,
    list_reservations,
    check_availability,
]
TOOLS_VISITANTES = [authorize_visit, list_visitors]
TOOLS_REGULAMENTO = [query_regulations]