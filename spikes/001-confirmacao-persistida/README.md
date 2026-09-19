# Spike 001 — Confirmação de tools persistida (ADK 2.9.2 + SQLite)

## Pergunta

Como reagir a uma confirmação pendente de tool (Garantia 1) numa API própria,
com sessão persistida em SQLite (Garantia 3), de forma que a resposta chegue
ao agente certo — e qual topologia de agentes torna isso determinístico?

## Como rodar

```
uv run python spikes/001-confirmacao-persistida/main.py
```

Usa um modelo fake registrado no registry do ADK (`aurora-fake-main`,
`aurora-fake-reservas`). Não precisa de `GEMINI_API_KEY`.

## O que foi descoberto sobre o ADK 2.9.2

1. **Mecanismo de confirmação**: tool com `FunctionTool(fn, require_confirmation=True)`
   (booleana) ou chamando `tool_context.request_confirmation(hint=..., payload=...)`
   dentro da tool (avançada). Em ambos os casos o ADK emite, na sessão, uma
   chamada de função sintética `adk_request_confirmation` cujos args contêm
   `originalFunctionCall` (nome/args da tool) e `toolConfirmation` (hint).
2. **Como ler a pendência**: eventos com `actions.requested_tool_confirmations`
   (dict chaveado pelo id da chamada ORIGINAL da tool, presente no evento que
   carrega a resposta da tool). A chamada que DEVEMOS responder é o FC
   `adk_request_confirmation` (long-running), num evento separado.
3. **Como responder (retomar)**: `Runner.run_async(..., new_message=Content(role='user',
   parts=[Part(function_response=FunctionResponse(id=<id do adk_request_confirmation>,
   name='adk_request_confirmation', response={'confirmed': bool,
   'payload': {...}}))]))`. Exatamente o que o CLI oficial do ADK faz
   (`google/adk/cli/cli.py`). O `id` da FunctionResponse resolve a invocation
   (`_resolve_invocation_id_from_fr`), e `_resolve_confirmation_targets`
   re-executa a tool confirmada UMA vez (valida nome/args/id contra o histórico).
4. **Persistência**: `DatabaseSessionService(db_url='sqlite+aiosqlite:///...')`
   (driver async OBRIGATÓRIO; `sqlite://` falha). `Runner(auto_create_session=True)`.
   Sessão, eventos e pendências sobrevivem ao restart do processo.
5. **Tool de transferência em 2.9.2** se chama `transfer_to_agent` com
   parâmetro `agent_name` (não `transfer_to_<nome>`); sub_agents em modo
   `chat` ganham esse tool automaticamente.
6. **Entre agentes** (transferência), o histórico chega ao outro agente como
   TRANSCRIÇÃO em texto ("For context: ..."), não como partes function_response.

## Descoberta central: topologia

Topologia **transfer_to_agent** (sub_agents modo `chat`): a retomada da
confirmação é NÃO-DETERMINÍSTICA. Em 8 repetições: 5/8 OK, e entre os erros
havia no-op silencioso (0 eventos) e `ValueError: Agent 'especialista_reservas'
cannot transfer to itself`. Mesmo com Runner novo a cada resposta, ficou em
4/8. Reproduzível em várias mensagens e em processo limpo.

Topologia **single_turn** (sub_agent com `mode='single_turn'`): o framework
expõe o especialista como TOOL inline do principal (mesma sessão, mesmo
invocation). A confirmação pedida pelo especialista retoma corretamente:
- mesmo runner: 10/10 (sempre executa exatamente 1x);
- após "reinício" (novo Runner + novo session service no mesmo arquivo): 10/10.

Aviso benigno observado no reinício: "Dropping function responses with no
matching function call" (a resposta duplicada é descartada; execução segue 1x).

## Veredito: VALIDADO (com restrição de topologia)

### O que funciona
- Confirmar/negar tool via FunctionResponse → `run_async`; tool executada
  exatamente 1x na aprovação; negação não grava.
- Reenvio da mesma resposta não re-executa a tool (a API deve mapear o 409
  com a própria tabela de pendentes).
- Tudo funciona com sessão persistida em SQLite e após reinício da API —
  desde que a topologia seja single_turn.
- `requested_tool_confirmations` + args do FC `adk_request_confirmation`
  fornecem hint/payload/originalFunctionCall para montar `acao`/`detalhes`.

### O que não funciona / não é confiável
- Retomada de confirmação em topologia com `transfer_to_agent`: flaky
  (no-op silencioso ou erro de auto-transferência), mesmo com runner novo.

### Recomendação para o build real
- **Topologia**: agente principal + especialistas com `mode='single_turn'`
  (sem transfer_to_agent). Cada especialista é uma tool do principal.
  Acionamento: o modelo do principal chama `<nome_do_especialista>(request=...)`.
- **Mecanismo**: tools de escrita com `tool_context.request_confirmation`
  (payload) para cobrança/visita; `require_confirmation=True` como alternativa
  booleana. Pendência da API = função FC id + dados extraídos do evento.
- **Retomada**: `run_async(new_message=FunctionResponse)` — sem `invocation_id`
  explícito (ele é ignorado quando a mensagem tem FunctionResponse; a resolução
  é pelo id do FC).
- **Sessões**: `DatabaseSessionService` com `sqlite+aiosqlite://`.
- **Loop de segurança (importante p/ produção)**: se o modelo emitir nome de
  tool inexistente repetidamente, o ADK estoura `LlmCallsLimitExceededError`
  (limite padrão 500) — sem erro de servidor no fluxo do avaliador, mas
  convém modelar para o roaming normal do Gemini.