"""Tools de negocio do Residencial Aurora.

Contratos de desenho:
- O apartamento NUNCA chega como argumento do modelo: se le do estado da
  sessão (`tool_context.state['apartamento']`); a API o fixa na criação.
- `reservar_area` usa confirmação AVANÇADA (`request_confirmation` com
  payload): o payload contém os detalhes exatos que o morador aprova, e a
  gravação usa os valores do payload (não os args do modelo).
- `cancelar_reserva` usa confirmação BOOLEANA (`require_confirmation=True`).
- Leituras (listar, disponibilidade, regulamento) não exigem confirmação.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools import FunctionTool, ToolContext

from ..config import cargar_area_ids
from ..storage import reservas_store, visitantes_store
from . import regulamento

DATA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

AREAS_VALIDAS = cargar_area_ids()


def _apartamento(tool_context: ToolContext) -> str:
  """Apartamento da sessão; o modelo nunca o elige (viene do contexto)."""
  return str(tool_context.state.get("apartamento") or "")


def _validar_area(area: str) -> str | None:
  return area if area in AREAS_VALIDAS else None


def _validar_data(data: str) -> str | None:
  return data if DATA_RE.match(data or "") else None


# ---------------------------------------------------------------- reservas

async def reservar_area(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
  """Reserva uma área comum do apartamento (gera cobrança -> confirmação)."""
  apartamento = _apartamento(tool_context)
  tc = tool_context.tool_confirmation
  if tc is None:
    tool_context.request_confirmation(
        hint=f"Reservar {area} para {data}? Gera cobrança para o {apartamento}.",
        payload={"area": area, "data": data, "apartamento": apartamento},
    )
    return {"status": "aguardando_confirmacao", "mensagem": "Confirmação pendente."}
  if not tc.confirmed:
    return {"status": "recusada_pelo_morador", "mensagem": "Reserva cancelada."}
  # gravação com os valores que o morador aprovou (não com os args do modelo).
  aprovado = tc.payload or {}
  area = aprovado.get("area") or area
  data = aprovado.get("data") or data
  apartamento = aprovado.get("apartamento") or apartamento
  if not (_validar_area(area) and _validar_data(data)):
    return {"status": "requisitos_invalidos",
            "mensagem": "Dados aprovados não passam validação (área ou data)."}
  r = await reservas_store.reservar(apartamento, area, data)
  if not r.ok:
    return {"status": "ocupada",
            "mensagem": f"O {area} na {data} já está reservado por outro apartamento."}
  return {"status": "ok", "codigo": r.reserva.codigo, "area": r.reserva.area,
          "data": r.reserva.data, "apartamento": apartamento}


async def cancelar_reserva(codigo: str, tool_context: ToolContext) -> dict[str, Any]:
  """Cancela uma reserva própria; exige confirmação (booleana)."""
  apartamento = _apartamento(tool_context)
  ok = await reservas_store.cancelar(apartamento, codigo)
  if not ok:
    return {"status": "inexistente",
            "mensagem": "Não encontrei ninguna reserva com esse código para o seu apartamento."}
  return {"status": "ok", "codigo": codigo, "mensagem": "Reserva cancelada."}


async def listar_reservas(tool_context: ToolContext) -> dict[str, Any]:
  """Lista as reservas ativas do apartamento da sessão."""
  apartamento = _apartamento(tool_context)
  return {"apartamento": apartamento,
          "reservas": await reservas_store.listar_apartamento(apartamento)}


async def esta_libre(area: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
  """Consulta disponibilidade (não exige confirmação)."""
  libre = await reservas_store.esta_libre(area, data)
  return {"area": area, "data": data, "libre": libre}


# -------------------------------------------------------------- visitantes

async def autorizar_visita(nome: str, data: str, tool_context: ToolContext) -> dict[str, Any]:
  """Autoriza a entrada de um visitante do apartamento (não gera cobro)."""
  apartamento = _apartamento(tool_context)
  if not (_validar_data(data) and nome and nome.strip()):
    return {"status": "requisitos_invalidos", "mensagem": "Falta nome ou data."}
  await visitantes_store.autorizar(apartamento, nome.strip(), data)
  return {"status": "ok", "apartamento": apartamento, "nome": nome.strip(), "data": data}


async def listar_visitantes(tool_context: ToolContext) -> dict[str, Any]:
  """Lista os visitantes autorizados do apartamento da sessão."""
  apartamento = _apartamento(tool_context)
  return {"apartamento": apartamento,
          "visitantes": await visitantes_store.listar_apartamento(apartamento)}


# ------------------------------------------------------------- regulamento

async def consultar_regulamento(consulta: str, tool_context: ToolContext) -> dict[str, Any]:
  """Busca regras relevantes do regulamento interno para a consulta."""
  del tool_context
  return {"consulta": consulta, "resposta": regulamento.consultar(consulta)}


# ------------------------------------------------------------------ tools

TOOLS_RESERVAS = [
    reservar_area,
    FunctionTool(cancelar_reserva, require_confirmation=True),
    listar_reservas,
    esta_libre,
]
TOOLS_VISITANTES = [autorizar_visita, listar_visitantes]
TOOLS_REGULAMENTO = [consultar_regulamento]