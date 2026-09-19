"""Extração de confirmações pendentes e construção da resposta de retomada.

Achados do spike 001 (ver spikes/001-confirmacao-persistida/README.md):
- A pendência aparece em DOIS pontos do stream de eventos: nas chaves de
  `actions.requested_tool_confirmations` (id da chamada ORIGINAL da tool, no
  evento com a resposta da tool) e na chamada sintética
  `adk_request_confirmation` (a que se deve responder, em outro evento).
- A retomada se faz com um FunctionResponse ao id de `adk_request_confirmation`
  como `new_message` de `run_async` (sem `invocation_id` explícito).
"""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.events import Event
from google.genai import types

CONFIRMACAO = "adk_request_confirmation"


@dataclass
class Pendencia:
  id_para_responder: str  # id do FC adk_request_confirmation
  acao: str               # hint ou nome da tool original
  detalhes: dict          # payload aprovável (se houver) ou args originais
  original: dict          # originalFunctionCall (nome/args/id)


def achar_pendente(events: list[Event]) -> Pendencia | None:
  """Busca a confirmação pendente em um turno; None se não houver."""
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
      return Pendencia(
          id_para_responder=fc.id,
          acao=tc.get("hint") or f"tool:{orig.get('name', '?')}",
          detalhes=tc.get("payload") or dict(args.get("args") or {}),
          original=orig,
      )
  return None


def resposta_confirmacao(
    fc_id: str, confirmado: bool, payload: dict | None = None
) -> types.Content:
  """Content de retomada: responde ao FC adk_request_confirmation."""
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


def mensagem_texto(texto: str) -> types.Content:
  return types.Content(role="user", parts=[types.Part(text=texto)])