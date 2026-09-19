"""Serialização de eventos ADK para JSON (GET /sessoes/{id}/eventos).

Inclui o que importa para a verificação do avaliador: autor, textos,
chamadas de tool com seus argumentos, respostas de tool, marcadores de
confirmação pendente e fim de turno. O evento sintético de re-ancoragem
(author=main_agent, sem conteúdo) aparece como um evento vazio após uma
confirmação (mecanismo documentado no spike 002 e no README).
"""

from __future__ import annotations

from google.adk.events import Event


def event_to_dict(event: Event) -> dict:
    """Converte um Event do ADK em dict JSON-serializable."""
    texts: list[str] = []
    function_calls: list[dict] = []
    function_responses: list[dict] = []
    if event.content and event.content.parts:
        for part in event.content.parts:
            if part.text:
                texts.append(part.text)
            if part.function_call:
                function_calls.append({
                    "name": part.function_call.name,
                    "id": str(part.function_call.id),
                    "args": dict(part.function_call.args or {}),
                })
            if part.function_response:
                function_responses.append({
                    "name": part.function_response.name,
                    "id": str(part.function_response.id),
                    "response": part.function_response.response,
                })
    requested: list[str] = []
    if event.actions and event.actions.requested_tool_confirmations:
        requested = sorted(str(k) for k in event.actions.requested_tool_confirmations)
    return {
        "id": str(event.id),
        "author": event.author,
        "timestamp": event.timestamp,
        "end_of_turn": bool(event.actions and event.actions.end_of_agent),
        "texts": texts,
        "function_calls": function_calls,
        "function_responses": function_responses,
        "requested_tool_confirmations": requested,
        "long_running_tool_ids": sorted(str(v) for v in (event.long_running_tool_ids or [])),
    }