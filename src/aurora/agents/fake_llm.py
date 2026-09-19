"""Modelo fake determinista para verificação local (sem chave de API).

Registrado como 'aurora-fake-main', 'aurora-fake-reservations',
'aurora-fake-visitors' e 'aurora-fake-regulations'. Imita o comportamento
esperado do Gemini para que a API possa ser testada offline de ponta a
ponta.

Topologia single_turn: os especialistas são expostos como TOOLS inline do
principal; o nome da tool = nome do agente e o argumento é `request`.
Regras (por prioridade):
1. Se alguma tool de negócio já respondeu no turno atual -> retornar o
   payload como texto (relay), para que 'resposta' da API leve o conteúdo.
2. Senão, se há FunctionResponse de 'adk_request_confirmation' no turno
   atual -> fim (a confirmação foi aplicada; não reemite a tool).
3. Principal -> tool do especialista correto segundo o texto do morador.
4. Especialista -> tool concreta segundo o texto do morador.

As regras 1 e 2 são escopadas ao TURNO ATUAL (`_fr_in_current_turn`), não à
sessão inteira: os especialistas usam `include_contents="default"`, então o
histórico completo (com o FR de confirmações já resolvidas) está sempre no
pedido — checar a sessão inteira faria o especialista responder "Feito!" em
todo turno seguinte e nunca mais chamar tool.
"""

from __future__ import annotations

import re
import unicodedata
from typing import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

CONFIRMATION = "adk_request_confirmation"

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CODE_RE = re.compile(r"RSV-\d+")

AREAS = ("salao-de-festas", "quadra", "piscina", "churrasqueira", "academia")

SPECIALISTS = ("reservations_agent", "visitors_agent", "regulations_agent")

BUSINESS_TOOLS = (
    "book_area", "cancel_reservation", "list_reservations", "check_availability",
    "authorize_visit", "list_visitors", "query_regulations",
)

ROUTING_MAIN = {
    "reservations_agent": ("reserv", "cancele", "cancel", "salao", "quadra",
                           "libre", "ocupada", "disponib", "listar", "quais reservas"),
    "visitors_agent": ("visitante", "visita", "autoriz", "libera", "entrada",
                       "registra"),
    "regulations_agent": ("piscina", "regulamento", "regra", "norma", "horario",
                          "silencio", "ruido", "mascota", "animal", "lixo",
                          "obra", "mudanza", "garaje", "estacionamiento"),
}

KEYS_CANCEL = ("cancel", "cancele")
KEYS_AVAIL = ("libre", "ocupada", "disponib", "esta livre")
KEYS_LIST_R = ("lista", "quais", "listar")
KEYS_AUTORIZA = ("autoriz", "libera", "entrada", "registra", "nova")
KEYS_LIST_V = ("quais visitantes", "lista", "listar")


def _norm(text: str) -> str:
    """Minúsculas, sem acentos, só alfanuméricos e espaços."""
    nfkd = unicodedata.normalize("NFKD", text or "").lower()
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only)


class FakeLlm(BaseLlm):
    @classmethod
    def supported_models(cls) -> list[str]:
        return ["aurora-fake-main", "aurora-fake-reservations",
                "aurora-fake-visitors", "aurora-fake-regulations"]

    # --------------------------------------------------------------- util

    def _last_user_text(self, llm_request: LlmRequest) -> str:
        for content in reversed(llm_request.contents):
            if content.role != "user":
                continue
            texts = [p.text or "" for p in (content.parts or []) if p.text]
            if texts and not texts[0].startswith("For context"):
                return texts[0]
        return ""

    def _has_fr(self, llm_request: LlmRequest, name: str) -> bool:
        for content in llm_request.contents:
            for part in (content.parts or []):
                fr = part.function_response
                if fr is not None and fr.name == name:
                    return True
        return False

    def _fr_in_current_turn(self, llm_request: LlmRequest, names: tuple[str, ...]) -> bool:
        for content in reversed(list(llm_request.contents)):
            for part in (content.parts or []):
                fr = part.function_response
                if fr is not None and fr.name in names:
                    return True
            if content.role == "user":
                texts = [p.text or "" for p in (content.parts or []) if p.text]
                if texts and not texts[0].startswith("For context"):
                    return False
        return False

    def _last_fr_response(self, llm_request: LlmRequest, names: tuple[str, ...]):
        for content in reversed(list(llm_request.contents)):
            for part in reversed(content.parts or []):
                fr = part.function_response
                if fr is not None and fr.name in names:
                    return fr.response
            if content.role == "user":
                texts = [p.text or "" for p in (content.parts or []) if p.text]
                if texts and not texts[0].startswith("For context"):
                    return None
        return None

    def _response(self, text: str = "", fc: types.FunctionCall | None = None) -> LlmResponse:
        parts = [types.Part(text=text)] if text else []
        if fc is not None:
            parts.append(types.Part(function_call=fc))
        return LlmResponse(partial=False, content=types.Content(role="model", parts=parts))

    @staticmethod
    def _area_in(text: str) -> str:
        norm = _norm(text).replace(" ", "")
        for area in AREAS:
            if _norm(area).replace(" ", "") in norm:
                return area
        return "salao-de-festas"

    def _date_in(self, text: str) -> str:
        m = DATE_RE.search(text)
        return m.group(0) if m else "2030-04-20"

    @staticmethod
    def _has_any(text: str, keys: tuple[str, ...]) -> bool:
        norm = _norm(text)
        return any(k in norm for k in keys)

    # ------------------------------------------------------------ domains

    def _reservations_fc(self, text: str) -> types.FunctionCall:
        if self._has_any(text, KEYS_CANCEL):
            m = CODE_RE.search(text)
            return types.FunctionCall(
                name="cancel_reservation", args={"codigo": m.group(0) if m else "RSV-1377"})
        if self._has_any(text, KEYS_AVAIL):
            return types.FunctionCall(
                name="check_availability",
                args={"area": self._area_in(text), "data": self._date_in(text)})
        if self._has_any(text, KEYS_LIST_R):
            return types.FunctionCall(name="list_reservations", args={})
        return types.FunctionCall(
            name="book_area",
            args={"area": self._area_in(text), "data": self._date_in(text)})

    def _visitors_fc(self, text: str) -> types.FunctionCall:
        if self._has_any(text, KEYS_AUTORIZA) and not self._has_any(text, KEYS_LIST_V):
            m = re.search(r"[A-ZÁÉÍÓÚ][a-záéíóúñ]+ [A-ZÁÉÍÓÚ][a-záéíóúñ]+", text)
            name = m.group(0) if m else "Joana Ribeiro"
            return types.FunctionCall(
                name="authorize_visit", args={"nome": name, "data": self._date_in(text)})
        return types.FunctionCall(name="list_visitors", args={})

    # ----------------------------------------------------------- generate

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        model = llm_request.model or ""
        text = self._last_user_text(llm_request)

        if self._fr_in_current_turn(llm_request, BUSINESS_TOOLS):
            payload = self._last_fr_response(llm_request, BUSINESS_TOOLS)
            yield self._response(text=repr(payload) if payload is not None else
                                 "Pronto! Ação registrada.")
            return

        if self._fr_in_current_turn(llm_request, (CONFIRMATION,)):
            yield self._response(text="Feito! Confirmação aplicada conforme o seu pedido.")
            return

        if model == "aurora-fake-main":
            if self._fr_in_current_turn(llm_request, SPECIALISTS):
                resp = self._last_fr_response(llm_request, SPECIALISTS)
                result = ""
                if isinstance(resp, dict):
                    result = str(resp.get("result", resp))
                yield self._response(
                    text=result or "Encaminhado: resposta do especialista.")
                return
            for name, keys in ROUTING_MAIN.items():
                if self._has_any(text, keys):
                    target = name
                    break
            else:
                target = "regulations_agent"
            yield self._response(fc=types.FunctionCall(
                name=target, args={"request": text}))
            return

        if model == "aurora-fake-reservations":
            yield self._response(fc=self._reservations_fc(text))
            return
        if model == "aurora-fake-visitors":
            yield self._response(fc=self._visitors_fc(text))
            return
        # aurora-fake-regulations
        yield self._response(fc=types.FunctionCall(
            name="query_regulations", args={"consulta": text}))
        return