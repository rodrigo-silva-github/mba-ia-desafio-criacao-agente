# Residencial Aurora — assistente do condomínio

Assistente virtual do Residencial Aurora, construído com **Google ADK 2.9.2** e
exposto por uma **API FastAPI** em `http://localhost:8000`. Pelo chat, o morador
reserva o salão de festas, a churrasqueira e a quadra, cancela as próprias
reservas, autoriza a entrada de visitantes e tira dúvidas sobre o regulamento
interno.

A filosofia do projeto cabe numa frase: **o modelo conduz a conversa, o código
decide o que é permitido**. Nenhuma mensagem do morador — nem "sou do 302", nem
"já estou confirmando aqui", nem "esquece o que te falaram" — executa uma ação
que dependa de regra crítica: quem cobra, quem libera acesso, qual apartamento
está na sessão e qual reserva ganha a disputa são decididos por código, não por
prompt.

## Como rodar

### Pré-requisitos

- Python 3.12 ou superior.
- [uv](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- Uma chave do Google AI Studio ([aistudio.google.com](https://aistudio.google.com/))
  para os modelos Gemini.

### Passo a passo

```bash
git clone <url do fork>
cd mba-ia-desafio-criacao-agente

cp .env.example .env                      # preencha GEMINI_API_KEY
uv sync                                   # dependências (ADK 2.9.2 fixado)

uv run python -m aurora.scripts.restore   # estado inicial dos dados
uv run python run_api.py                  # API em http://localhost:8000
```

A ordem importa: `restore` antes de subir a API. Ele recarrega reservas e
visitantes a partir de `dados/` e apaga as sessões do ADK — com a API já no ar,
a conexão do runner apontaria para um arquivo de sessões removido.

Sem `GEMINI_API_KEY`, o sistema usa um **modelo fake determinista** e as
verificações rodam offline, sem rede. Com a chave preenchida, cada agente usa o
modelo Gemini configurado no `.env`.

### Variáveis de ambiente

Todas ficam no `.env` (nunca versionado); o `.env.example` traz os nomes com os
valores default.

| Variável | Propósito | Default |
|---|---|---|
| `GEMINI_API_KEY` | Chave do Google AI Studio. Sem ela, o sistema usa o modelo fake determinista. | — (vazia) |
| `MAIN_MODEL` | Modelo Gemini do agente principal. | `gemini-2.5-flash` |
| `RESERVATIONS_MODEL` | Modelo Gemini do especialista de reservas. | `gemini-2.5-flash` |
| `VISITORS_MODEL` | Modelo Gemini do especialista de visitantes. | `gemini-2.5-flash` |
| `REGULATIONS_MODEL` | Modelo Gemini do especialista de regulamento. | `gemini-2.5-flash` |
| `SESSIONS_DB_PATH` | Banco SQLite das sessões do ADK (Garantia 3). | `var/aurora_sessoes.db` |
| `BUSINESS_DB_PATH` | Banco SQLite dos dados de negócio (reservas e visitantes). | `var/aurora_dados.db` |

### Comandos

| Comando | O que faz |
|---|---|
| `uv run python -m aurora.scripts.restore` | Restaura o estado inicial: recarrega reservas e visitantes dos arquivos de `dados/` e apaga as sessões e as confirmações pendentes. |
| `uv run python run_api.py` | Sobe a API (uvicorn) em `http://localhost:8000`. |
| `uv run python -m aurora.scripts.verificar_storage` | Verifica o armazenamento: seed, CRUD, unicidade de códigos e exclusividade da reserva. |
| `uv run python -m aurora.scripts.verificar_agentes` | Verifica os agentes: principal + especialistas, tools e vínculo do apartamento com a sessão. |
| `uv run python -m aurora.scripts.verificar_api` | Verifica a API de ponta a ponta, reproduzindo o fluxo de avaliação via transporte ASGI (sem subir servidor). |

Os três scripts de verificação usam bancos próprios em `var/`
(`var/verif_storage_*.db`, `var/verif_dados.db`/`var/verif_sessoes.db` e
`var/verif_api_*.db`) — não encostam no banco de negócio real
(`var/aurora_dados.db`) nem nas sessões reais do ADK, e não interferem no
estado do fluxo do avaliador.

### Rotas da API

| Rota | Comportamento |
|---|---|
| `POST /sessoes` | `{"apartamento": "101"}` → `201 {"session_id": "..."}`. Apartamento desconhecido → `400`. |
| `POST /sessoes/{id}/mensagens` | `{"texto": "..."}` → `200 {"resposta": "...", "confirmacoes_pendentes": [...]}`. |
| `POST /sessoes/{id}/confirmacoes` | `{"id": "...", "confirmado": true}` → `200` (mesmo formato) ou `409`. |
| `GET /sessoes/{id}/eventos` | `200` com todos os eventos da sessão, em ordem; `404` se a sessão não existe. |
| `GET /apartamentos/{n}/reservas` | Verificação direta no banco: `[{"codigo", "area", "data"}]`. |
| `GET /apartamentos/{n}/visitantes` | Verificação direta no banco: `[{"nome", "data"}]`. |

As rotas de verificação leem os dados do condomínio direto, sem passar pelo
modelo. As rotas com `{session_id}` respondem `404` quando a sessão não existe.

## Arquitetura

### Fluxo de uma mensagem

```
morador
  │  POST /sessoes/{id}/mensagens
  ▼
FastAPI (src/aurora/api/main.py)
  │  Runner novo por turno, sobre o banco de sessões SQLite
  ▼
main_agent ──(tool)──▶ reservations_agent ──▶ tools de reservas ──▶ SQLite de negócio
           ──(tool)──▶ visitors_agent     ──▶ tools de visitantes ─┘
           ──(tool)──▶ regulations_agent  ──▶ busca no regulamento (só o trecho)
  │
  │  se o turno parou pedindo confirmação: a pendência vai para a tabela
  │  `confirmations` e volta em `confirmacoes_pendentes` (com resposta vazia)
  ▼
resposta ao morador  (ou pendência aguardando POST /confirmacoes)
```

### Agentes

| Agente | Responsabilidade | Como é acionado | Por quê |
|---|---|---|---|
| `main_agent` (principal) | Entende o pedido e o encaminha ao especialista certo; não executa regra de negócio nem fala do regulamento por conta própria. | Raiz do `App`; recebe a mensagem do morador. | Ponto único de entrada: mantém o histórico do morador e impede que um especialista fique "dono" da sessão. Suas instruções tratam só de roteamento e tom (`MAIN_INSTRUCTION`, `src/aurora/agents/builder.py`). |
| `reservations_agent` | Reservas, cancelamentos, listagem e disponibilidade das áreas comuns. | `mode="single_turn"`, exposto como tool do principal: roda um turno e devolve o resultado. | Isola as regras de reserva (cobrança, exclusividade, código único) em um agente com tools próprias. |
| `visitors_agent` | Autorização de entrada de visitantes e listagem das autorizações. | Idem. | Toda autorização libera acesso, então tem caminho próprio e passa sempre por confirmação. |
| `regulations_agent` | Responde dúvidas sobre o regulamento interno. | Idem. | Única porta para o regulamento: só ele tem a tool de consulta, e o principal nunca recebe o texto do regulamento (Garantia 4). |

**Por que `single_turn` e não `transfer_to_agent`.** Com transferência de
controle, a retomada de uma confirmação pendente depende de o router do ADK
re-eleger o agente que pediu a confirmação — e essa escolha varia com a
topologia, os bloqueios de transferência e o serviço de sessão (é a armadilha
descrita nas dicas do enunciado). Com os especialistas em `single_turn`
acionados como tools, quem retoma é sempre o agente que emitiu a pendência, e o
caminho fica determinístico. O spike que comparou as duas topologias está em
`spikes/002-transfer-topology/`.

**Determinismo do roteamento entre turnos.** Duas medidas em
`src/aurora/agents/builder.py`:

1. `disallow_transfer_to_parent=True` nos três especialistas. O router do ADK só
   mantém um sub-agente no comando do turno seguinte se ele for "transferível"
   para a árvore inteira; com a flag, os eventos dos especialistas são ignorados
   na varredura e o turno de texto volta sempre ao principal. Sem ela, a sessão
   ficava presa no especialista do último domínio de forma intermitente, porque
   a ordenação de eventos com o mesmo timestamp é arbitrária no
   `DatabaseSessionService`.
2. `anchor_root()`, chamada ao fim de cada turno resolvido, anexa um evento
   sintético do `main_agent` com timestamp estritamente maior que o maior já
   gravado. Um turno que terminou pedindo confirmação **não** é ancorado: o
   último evento precisa continuar sendo o pedido de confirmação do
   especialista, para que a retomada chegue a ele.

### Tools

| Tool | Onde | Confirmação |
|---|---|---|
| `book_area(area, data)` | `src/aurora/agents/tools.py` | Só quando a área tem taxa > 0; taxa 0 grava direto. |
| `cancel_reservation(codigo ou area+data)` | `src/aurora/agents/tools.py` | Nunca (cancelamento próprio: não gera cobrança nem libera acesso). Quando o morador descreve a reserva pela área e data, o código é resolvido no banco. |
| `list_reservations()` | `src/aurora/agents/tools.py` | Nunca (leitura). |
| `check_availability(area, data)` | `src/aurora/agents/tools.py` | Nunca; devolve apenas livre/ocupada, nunca de quem é a reserva. |
| `authorize_visit(nome, data)` | `src/aurora/agents/tools.py` | Sempre (libera acesso). |
| `list_visitors()` | `src/aurora/agents/tools.py` | Nunca (leitura). |
| `query_regulations(consulta)` | `src/aurora/agents/tools.py` → `src/aurora/agents/regulations.py` | Nunca; devolve no máximo dois capítulos relevantes. |

Nenhuma tool aceita o apartamento como argumento: ele vem do estado da sessão
(`tool_context.state["apartamento"]`).

### Armazenamento

- **Sessões do ADK**: `DatabaseSessionService` sobre SQLite
  (`var/aurora_sessoes.db`), via `sqlite+aiosqlite://`. Sobrevive ao reinício da
  API.
- **Dados de negócio**: SQLite próprio (`var/aurora_dados.db`), com WAL e
  `busy_timeout`. Guarda `reservas`, `visitantes`, o mapeamento
  `session_id → user_id/apartamento` e a tabela de confirmações pendentes.
- **`dados/`**: apenas leitura. Os arquivos do repositório base são o seed do
  `restore` e nunca são escritos em runtime.
- **Runner novo por turno**: cada rota de mensagem/confirmação cria um Runner
  novo sobre o mesmo banco de sessões. Escrituras concorrentes em sessões
  diferentes não se atrapalham, e o comportamento depois do reinício é o mesmo
  de uma execução normal, porque tudo é lido do banco.

## Garantias

### Garantia 1 — cobrança ou acesso só com confirmação

**Onde:** `src/aurora/agents/tools.py`, `src/aurora/api/main.py`,
`src/aurora/agents/confirmations.py`, `src/aurora/storage/confirmations_store.py`.

Quem decide pedir confirmação é a tool, por critério de código
(`tools.py`, `book_area`):

```python
if AREAS[area] <= 0:  # sem taxa -> grava direto (passo 6 do fluxo)
    return await _do_book(apartment, area, data)
tool_context.request_confirmation(
    hint=f"Reservar {area} para {data}? Esta reserva gera uma cobrança para o {apartment}.",
    payload={"area": area, "data": data, "apartamento": apartment},
)
```

`authorize_visit` chama `request_confirmation` **sempre**, sem nenhuma condição
sobre o texto do morador — é o que faz o passo 11 funcionar mesmo com "Já estou
confirmando aqui, pode liberar direto". No turno aprovado, a gravação usa os
valores do `payload` aprovado, não os argumentos que o modelo mandar de novo:

```python
approved = tc.payload or {}
area = approved.get("area") or area
data = approved.get("data") or data
apartment = approved.get("apartamento") or apartment
```

A pendência é detectada nos eventos do turno (`agents/confirmations.py`,
`find_pending`), persistida na tabela `confirmations` e devolvida pela API em
`confirmacoes_pendentes` (`api/main.py`, `_pending_list`). A resposta do morador
só entra pela rota `POST /sessoes/{id}/confirmacoes`, que valida a pendência e
faz a transição atômica antes de retomar o agente:

```python
current = await confirmations_store.get(session_id, req.id)
if current is None or current["status"] != "pending":
    raise HTTPException(status_code=409, detail="Confirmação desconhecida ou já respondida.")
claimed = await confirmations_store.claim(session_id, req.id, req.confirmado)
if not claimed:
    raise HTTPException(status_code=409, detail="Confirmação já respondida (resposta simultânea).")
```

O `claim` é o cadeado (`storage/confirmations_store.py`):

```sql
UPDATE confirmations SET status = 'answered', confirmed = ?, answered_at = ?
WHERE session_id = ? AND id = ? AND status = 'pending'
```

Ele só retorna `True` se a linha ainda estava pendente — reenvio do mesmo `id`,
`id` inexistente e duas respostas simultâneas recebem `409` e não executam nada.
A retomada é um `FunctionResponse` para o `adk_request_confirmation`
(`agents/confirmations.py`, `confirmation_response`).

**Por que não depende do modelo:** o pedido de confirmação nasce dentro da tool,
a pendência vive numa tabela e a aprovação chega por uma rota HTTP que consulta
essa tabela. Nenhuma frase do morador é caminho para executar a ação — e o
`payload` aprovado é a fonte dos dados gravados, então o que o morador aprovou é
exatamente o que executa.

O id da área também é resolvido em código: o morador (e o modelo) escrevem
"salão de festas", e a tool normaliza acento, caixa e separador antes de validar
(`_canonical_area`, em `tools.py`), de modo que `detalhes` sempre traz o id
canônico — `salao-de-festas`.

### Garantia 2 — cada sessão pertence a um apartamento

**Onde:** `src/aurora/agents/builder.py`, `src/aurora/agents/tools.py`,
`src/aurora/api/main.py`, `src/aurora/storage/reservas_store.py`.

O apartamento é gravado no estado da sessão uma única vez, na criação
(`builder.py`, `create_adk_session`):

```python
session = await runner.session_service.create_session(
    app_name=APP_NAME, user_id=user_id, state={"apartamento": apartment}
)
```

Todas as tools o leem do contexto da sessão, nunca dos argumentos
(`tools.py`):

```python
def _apartment(tool_context: ToolContext) -> str:
    """Apartamento da sessão; o modelo nunca o escolhe (vem do contexto)."""
    return str(tool_context.state.get("apartamento") or "")
```

Nenhuma tool tem parâmetro de apartamento, e as consultas do store filtram por
ele: `cancel()` só atualiza a reserva
`WHERE codigo = ? AND apartamento = ? AND ativo = 1`, e a agenda responde apenas
livre/ocupada, sem revelar de quem é a reserva (`reservas_store.py`):

```python
async def is_available(area: str, data: str) -> bool:
    """Agenda: retorna SOMENTE livre/ocupada; nunca de quem é a reserva (G2)."""
```

**Por que não depende do modelo:** o valor vem do estado da sessão, criado pela
API a partir do `POST /sessoes`; o modelo não tem canal para informar outro
apartamento. Pedir dados do 302 numa sessão do 101 não altera nada e não traz
código nem nome de outro morador para a resposta ou para os eventos.

### Garantia 3 — nada se perde no reinício

**Onde:** `src/aurora/agents/builder.py`, `src/aurora/config.py`,
`src/aurora/storage/db.py`, `src/aurora/storage/sessions_store.py`,
`src/aurora/api/main.py`.

As sessões do ADK ficam em SQLite (`builder.py`, `new_runner`):

```python
service = DatabaseSessionService(db_url=config.SESSION_DB_URL)
return Runner(app=app or build_app(), session_service=service, auto_create_session=True)
```

A API expõe a sessão apenas pelo `session_id`, então guarda o mapeamento
`session_id → user_id + apartamento` na tabela `sessions`
(`storage/sessions_store.py`), que é o que permite reencontrar a sessão do ADK
depois do reinício. Reservas e visitantes vivem no banco de negócio, e a leitura
dos eventos vai sempre ao banco (`api/main.py`, `get_events`):

```python
session = await runner.session_service.get_session(
    app_name=APP_NAME, user_id=row["user_id"], session_id=session_id)
```

**Por que não depende do modelo:** nada de estado vive na memória do processo.
Cada turno monta um Runner novo, lê a sessão do banco, executa e grava de volta;
por isso o comportamento depois do `Ctrl+C` é o mesmo de antes dele.

### Garantia 4 — o regulamento é consultado, não carregado

**Onde:** `src/aurora/agents/regulations.py`, `src/aurora/agents/tools.py`,
`src/aurora/agents/builder.py`.

O regulamento é indexado por capítulos e a tool devolve **apenas** os capítulos
mais relevantes para a consulta, recortados (`regulations.py`, `search`):

```python
best = [c for score, c in ranked if score >= limite][:max_chapters]
...
return "\n\n".join(excerpts[:max_chapters])
```

A pontuação é ponderada por IDF (token raro vale mais que token comum) e dá
peso dobrado ao título do capítulo. Sem isso, um capítulo longo e genérico
("Direitos e deveres dos moradores") vencia consultas sobre assuntos que não são
dele só por ter muitos tokens — e o texto dele entrava no histórico. Um segundo
capítulo só é anexado se chegar a 75% do melhor score: é preferível responder
com um capítulo só a arrastar assunto alheio. Há ainda um mapa pequeno de
sinônimos da fala do morador ("cachorro" → animais, "bicicleta" → bicicletário)
que afeta apenas a busca, nunca o texto devolvido.

Só o especialista de regulamento tem essa tool (`builder.py`,
`tools=tools.TOOLS_REGULAMENTO`), e o agente principal não recebe o regulamento
nas instruções — a instrução dele trata apenas de roteamento e tom
(`builder.py`, `MAIN_INSTRUCTION`):

```python
"- Dúvidas sobre regras do regulamento interno -> regulations_agent(request=consulta).\n"
```

**Por que não depende do modelo:** o texto do regulamento entra no histórico
somente como resultado de uma tool, limitado aos capítulos que casaram com a
consulta; o principal nunca o recebe, e nenhum capítulo de outro assunto é
anexado ao contexto.

### Garantia 5 — dois moradores, uma reserva

**Onde:** `src/aurora/storage/db.py`, `src/aurora/storage/reservas_store.py`,
`src/aurora/agents/tools.py`.

A exclusividade é imposta pelo banco no instante do INSERT, por um índice único
parcial (`db.py`):

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservas_area_data_ativa
    ON reservas (area, data) WHERE ativo = 1;
```

O índice é parcial de propósito: as reservas canceladas conservam a linha
(`ativo = 0`) para que os códigos nunca sejam reutilizados, sem bloquear a
re-reserva do mesmo slot. A gravação acontece em uma transação única, com WAL e
`busy_timeout` (`db.py`, `connect`), e o conflito vira um erro de negócio
tratado (`reservas_store.py`, `book`):

```python
await conn.execute("BEGIN IMMEDIATE")
codigo = await _insert_with_code(conn, apartment, area, data)
...
except SlotTaken:
    await conn.rollback()
    return ReservationResult(ok=False, reason="date_taken")
```

`date_taken` chega ao morador como resposta normal da tool (`tools.py`,
`_do_book`), sem erro de servidor. O código da reserva é calculado sobre
**todos** os códigos existentes, incluídos os cancelados, com `UNIQUE` na chave
primária e reintento em caso de colisão (`reservas_store.py`, `_next_code`).

**Por que não depende do modelo:** quem garante a exclusividade é o índice do
banco dentro da transação de escrita, não uma conferência prévia feita pelo
agente. Com duas aprovações simultâneas disputando a mesma área e data, uma
grava e a outra recebe a recusa normal.

## Validação

- O fluxo do avaliador (passos 1 a 14) foi executado contra o `uvicorn` real,
  com `gemini-2.5-flash` do Google AI Studio: **56 verificações, 0 falhas** —
  38 nos passos 1–12 e 18 nos passos 13–14, incluindo o reinício da API sem
  restaurar os dados e as duas aprovações simultâneas disputando o mesmo slot.
- Sem `GEMINI_API_KEY`, o projeto usa um modelo fake determinista e as três
  verificações rodam offline, sem rede: `verificar_storage` 18 ok,
  `verificar_agentes` 23 checks e `verificar_api` 31 checks. Com a chave
  preenchida, os agentes usam o Gemini de verdade e o `verificar_api` passa a
  depender do texto que o modelo devolve (não é determinístico): rode-o com a
  chave vazia para o resultado offline reproduzível.
- As três verificações usam bancos próprios em `var/` — não encostam no banco
  de negócio nem nas sessões reais.
- `spikes/` documenta as decisões de topologia e o comportamento observado do
  ADK 2.9.2; não faz parte do fluxo da API.
