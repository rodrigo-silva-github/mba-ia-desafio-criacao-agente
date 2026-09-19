"""Modelo fake deterministico para o spike 001 (nao usa chave de API).

Registrado como 'aurora-fake-main' e 'aurora-fake-reservas'.
Regras (por prioridade):
1. Ha resposta de confirmacao ('adk_request_confirmation') na sessao
   (parte function_response ou transcricao textual) -> texto final.
2. Alguma tool de negocio ja executou -> texto final.
3. Agente principal -> transfere via transfer_to_agent quando pedido.
4. Especialista -> chama a tool conforme o texto real do usuario.

Observacoes do ADK 2.9.2 descobertas no spike:
- Entre agentes (transferencia), o historico vem como TRANSCRICAO em texto
  ('For context: ...'), nao como partes function_response.
- A tool de transferencia se chama 'transfer_to_agent' com arg 'agent_name'.
"""

from typing import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

CONFIRMACAO = "adk_request_confirmation"
TRANSFERENCIA = "transfer_to_agent"
TOOLS = ("reservar_salao", "liberar_visitante")


class FakeLlm(BaseLlm):
  @classmethod
  def supported_models(cls) -> list[str]:
    return ["aurora-fake-main", "aurora-fake-reservas"]

  def _ultimo_texto_usuario_real(self, llm_request: LlmRequest) -> str:
    for content in reversed(llm_request.contents):
      if content.role != "user":
        continue
      textos = [p.text or "" for p in (content.parts or []) if p.text]
      if textos and not textos[0].startswith("For context"):
        return textos[0]
    return ""

  def _tem_fr(self, llm_request: LlmRequest, name: str) -> bool:
    for content in llm_request.contents:
      for part in (content.parts or []):
        fr = part.function_response
        if fr is not None and fr.name == name:
          return True
    return False

  def _tem_texto(self, llm_request: LlmRequest, trecho: str) -> bool:
    for content in llm_request.contents:
      for part in (content.parts or []):
        if part.text and trecho in part.text:
          return True
    return False

  def _resposta(self, texto: str = "", fc: types.FunctionCall | None = None) -> LlmResponse:
    parts = [types.Part(text=texto)] if texto else []
    if fc is not None:
      parts.append(types.Part(function_call=fc))
    return LlmResponse(partial=False, content=types.Content(role="model", parts=parts))

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    modelo = llm_request.model or ""
    texto = self._ultimo_texto_usuario_real(llm_request)

    # 1. Confirmacao respondida -> retomada concluida.
    confirmado = self._tem_fr(llm_request, CONFIRMACAO) or self._tem_texto(llm_request, CONFIRMACAO)
    if confirmado:
      yield self._resposta(texto="Acao executada conforme a confirmacao do usuario.")
      return

    # 2. Tool de negocio executou -> encerra turno.
    if any(self._tem_fr(llm_request, t) for t in TOOLS):
      yield self._resposta(texto="Pronto! Acao concluida.")
      return

    # 3. Agente principal: delega para o especialista conforme a topologia.
    if modelo == "aurora-fake-main":
      if any(self._tem_fr(llm_request, t) for t in ("especialista_reservas", TRANSFERENCIA)):
        yield self._resposta(texto="Encaminhei para o especialista.")
        return
      if "visitante" in texto or "especialista" in texto or "salao" in texto or "quadra" in texto:
        tools_dict = llm_request.tools_dict or {}
        if "especialista_reservas" in tools_dict:
          fc = types.FunctionCall(name="especialista_reservas", args={"request": f"O morador pediu: {texto}"})
        else:
          fc = types.FunctionCall(name=TRANSFERENCIA, args={"agent_name": "especialista_reservas"})
        yield self._resposta(fc=fc)
        return
      yield self._resposta(texto="Sou o assistente do Aurora. Posso delegar para o especialista de reservas.")
      return

    # 4. Especialista de reservas.
    if "visitante" in texto:
      fc = types.FunctionCall(name="liberar_visitante", args={"nome": "Joana Ribeiro", "data": "2030-04-21"})
      yield self._resposta(fc=fc)
      return
    if any(k in texto for k in ("reservar", "salao", "quadra")):
      fc = types.FunctionCall(name="reservar_salao", args={"area": "salao-de-festas", "data": "2030-04-20"})
      yield self._resposta(fc=fc)
      return
    yield self._resposta(texto="Sou o especialista de reservas.")