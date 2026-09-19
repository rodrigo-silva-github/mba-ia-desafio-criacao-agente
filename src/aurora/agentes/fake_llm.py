"""Modelo fake determinista para verificação local (sem chave de API).

Registrado como 'aurora-fake-main', 'aurora-fake-reservas',
'aurora-fake-visitantes' e 'aurora-fake-regulamento'. Imita o comportamento
esperado do Gemini (rotear ao especialista e chamar a tool correta) para que
a API possa ser testada de ponta a ponta offline.

Regras (por prioridade):
1. Há resposta de confirmação ('adk_request_confirmation') na sessão -> fim.
2. Alguna tool de negócio já executou -> fim.
3. Principal -> tool do especialista correspondente (single_turn).
4. Especialista -> chama a tool concreta segundo o texto real do morador.

Nomes das tools (topologia single_turn):
- especialista_reservas(request), especialista_visitantes(request),
  especialista_regulamento(request) no principal;
- reservar_area/cancelar_reserva/listar_reservas/esta_libre (reservas);
- autorizar_visita/listar_visitantes (visitantes);
- consultar_regulamento (regulamento).
"""

from __future__ import annotations

import re
from typing import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

CONFIRMACAO = "adk_request_confirmation"

DATA_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CODIGO_RE = re.compile(r"RSV-\d+")
NOME_RE = re.compile(r"[A-ZÁÉÍÓÚ][a-záéíóúñ]+ [A-ZÁÉÍÓÚ][a-záéíóúñ]+")

# Áreas do seed (para escolher a correta se o texto fala de uma).
AREAS = ("salao-de-festas", "quadra", "piscina", "churrasqueira", "academia")


class FakeLlm(BaseLlm):
  @classmethod
  def supported_models(cls) -> list[str]:
    return ["aurora-fake-main", "aurora-fake-reservas",
            "aurora-fake-visitantes", "aurora-fake-regulamento"]

  def _ultimo_texto(self, llm_request: LlmRequest) -> str:
    for content in reversed(llm_request.contents):
      if content.role != "user":
        continue
      textos = [p.text or "" for p in (content.parts or []) if p.text]
      if textos and not textos[0].startswith("For context"):
        return textos[0]
    return ""

  def _tem_fr(self, llm_request: LlmRequest, nombre: str) -> bool:
    for content in llm_request.contents:
      for part in (content.parts or []):
        fr = part.function_response
        if fr is not None and fr.name == nombre:
          return True
    return False

  def _fr_no_turno_atual(self, llm_request: LlmRequest, tools: tuple[str, ...]) -> bool:
    """Algún FR de tool neste turno (SEM mirar turnos anteriores).

    Varre de atrás para adiante: recolhe FRs até encontrar o último texto
    real do usuário (fronteira do turno); os FRs de turnos viejos fican
    antes dessa fronteira e não contam.
    """
    for content in reversed(list(llm_request.contents)):
      for part in (content.parts or []):
        fr = part.function_response
        if fr is not None and fr.name in tools:
          return True
      if content.role == "user":
        textos = [p.text or "" for p in (content.parts or []) if p.text]
        if textos and not textos[0].startswith("For context"):
          return False
    return False

  def _resposta(self, texto: str = "", fc: types.FunctionCall | None = None) -> LlmResponse:
    partes = [types.Part(text=texto)] if texto else []
    if fc is not None:
      partes.append(types.Part(function_call=fc))
    return LlmResponse(partial=False, content=types.Content(role="model", parts=partes))

  @staticmethod
  def _area_en(texto: str) -> str:
    for a in AREAS:
      if a in texto:
        return a
    return "salao-de-festas"

  def _data_en(self, texto: str) -> str:
    m = DATA_RE.search(texto)
    return m.group(0) if m else "2030-04-20"

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    modelo = llm_request.model or ""
    texto = self._ultimo_texto(llm_request)

    if self._tem_fr(llm_request, CONFIRMACAO):
      yield self._resposta(texto="Feito! Registrei a ação conforme a sua confirmação.")
      return
    for tool in ("reservar_area", "cancelar_reserva", "listar_reservas", "esta_libre",
                 "autorizar_visita", "listar_visitantes", "consultar_regulamento"):
      if self._tem_fr(llm_request, tool) and self._fr_no_turno_atual(llm_request, (tool,)):
        yield self._resposta(texto="Pronto! Ação registrada conforme o pedido.")
        return

    if modelo == "aurora-fake-main":
      if any(self._fr_no_turno_atual(llm_request, (t,)) for t in
             ("especialista_reservas", "especialista_visitantes", "especialista_regulamento")):
        yield self._resposta(texto="Encaminei o seu pedido ao especialista.")
        return
      nombres = {"especialista_reservas": ("reservar", "reserva", "salao", "quadra",
                                            "cancel", "cancelar", "libre",
                                            "disponible", "ocupada", "listar"),
                 "especialista_visitantes": ("visitante", "visita", "autorizar"),
                 "especialista_regulamento": ("regla", "regra", "regulamento", "norma",
                                              "piscina", "horario", "silencio", "ruido",
                                              "mascota", "animales", "lixo", "obra",
                                              "mudanza", "garaje", "estacionamiento")}
      objetivo = None
      for nombre, claves in nombres.items():
        if any(c in texto for c in claves or ()):
          objetivo = nombre
          break
      if objetivo is None:
        objetivo = "especialista_regulamento"
      fc = types.FunctionCall(name=objetivo, args={"request": texto})
      yield self._resposta(fc=fc)
      return

    if modelo == "aurora-fake-reservas":
      if "cancel" in texto or "cancelar" in texto or "cancelamento" in texto:
        m = CODIGO_RE.search(texto)
        codigo = m.group(0) if m else "RSV-1377"
        yield self._resposta(fc=types.FunctionCall(name="cancelar_reserva", args={"codigo": codigo}))
      elif any(k in texto for k in ("libre", "disponible", "ocupada", "disponibilidade")):
        yield self._resposta(fc=types.FunctionCall(
            name="esta_libre", args={"area": self._area_en(texto), "data": self._data_en(texto)}))
      elif any(k in texto for k in ("lista", "quais", "listar")):
        yield self._resposta(fc=types.FunctionCall(name="listar_reservas", args={}))
      else:
        yield self._resposta(fc=types.FunctionCall(
            name="reservar_area", args={"area": self._area_en(texto), "data": self._data_en(texto)}))
      return

    if modelo == "aurora-fake-visitantes":
      if any(k in texto for k in ("autoriza", "registra", "nova", "visitante")):
        m = NOME_RE.search(texto)
        nome = m.group(0) if m else "Joana Ribeiro"
        yield self._resposta(fc=types.FunctionCall(
            name="autorizar_visita", args={"nome": nome, "data": self._data_en(texto)}))
      else:
        yield self._resposta(fc=types.FunctionCall(name="listar_visitantes", args={}))
      return

    # aurora-fake-regulamento
    yield self._resposta(fc=types.FunctionCall(
        name="consultar_regulamento", args={"consulta": texto}))
    return