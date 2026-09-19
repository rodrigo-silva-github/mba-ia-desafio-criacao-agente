"""Extração de confirmação pendente e construção da resposta de retomada.

Achados do spike 001 (ver spikes/001-confirmacao-persistida/README.md):
- A pendência aparece em DOIS pontos do stream de eventos: nas chaves de
  `actions.requested_tool_confirmations` (id da chamada ORIGINAL da tool, no
  evento com a resposta da tool) e na chamada sintética
  `adk_request_confirmation` (a que se deve responder, em outro evento).
- A retomada se faz com um FunctionResponse ao id de `adk_request_confirmation`
  como `new_message` de `run_async` (sem `invocation_id` explícito).
- Reenviar o mesmo id não re-executa a tool (validação estrita do ADK); a
  API reforça isso com sua própria tabela de pendentes (409 garantido).
"""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.events import Event
from google.genai import types

CONFIRMATION_FC = "adk_request_confirmation"


@dataclass
class Pending:
    id_to_answer: str  # id do FC adk_request_confirmation
    action: str        # hint ou nome da tool original
    details: dict      # payload aprovável (se houver) ou args originais
    original: dict     # originalFunctionCall (nome/args/id)


def find_pending(events: list[Event]) -> Pending | None:
    """Busca a confirmação pendente em um turno; None se não houver."""
    original_ids: set[str] = set()
    for ev in events:
        reqs = ev.actions.requested_tool_confirmations if ev.actions else None
        if reqs:
            original_ids.update(reqs.keys())
    for ev in reversed(events):
        for fc in ev.get_function_calls():
            if fc.name != CONFIRMATION_FC or not fc.id:
                continue
            args = fc.args or {}
            original = args.get("originalFunctionCall") or {}
            if original_ids and original.get("id") not in original_ids:
                continue
            confirmation = args.get("toolConfirmation") or {}
            return Pending(
                id_to_answer=fc.id,
                action=confirmation.get("hint") or f"tool:{original.get('name', '?')}",
                details=confirmation.get("payload") or dict(args.get("args") or {}),
                original=original,
            )
    return None


def confirmation_response(
    fc_id: str, confirmed: bool, payload: dict | None = None
) -> types.Content:
    """Content de retomada: responde ao FC adk_request_confirmation."""
    response: dict = {"confirmed": confirmed}
    if payload is not None:
        response["payload"] = payload
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=fc_id, name=CONFIRMATION_FC, response=response
                )
            )
        ],
    )


def text_message(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])