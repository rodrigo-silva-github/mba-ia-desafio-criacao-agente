# Planejamento — Desafio "Regra é regra" (Residencial Aurora)

> Documento de trabalho. O entregável final é o `README.md` na raiz (seções
> Arquitetura, Garantias e Como rodar) substituindo este enunciado — este
> arquivo não faz parte da entrega e pode ser removido antes do push final.
>
> Todos os pontos de decisão estavam marcados como **[ABERTO]** no corpo do
> texto e consolidados na seção "Pontos de decisão em aberto" no fim deste
> documento. **Não há mais nenhum em aberto**: as decisões D1–D9d estão
> resolvidas na §9, com o arquivo onde cada uma vive.

---

## 1. Objetivo em uma frase

Construir, com Google ADK 2.x, um assistente de condomínio exposto por API
(FastAPI em `http://localhost:8000`) onde **o modelo conduz a conversa mas as
regras críticas vivem no código** — confirmação de cobrança/acesso, sessão
presa a um apartamento, persistência, regulamento consultado via tool e
exclusividade de reserva no instante da gravação.

## 2. Restrições e tecnologias (checklist fixo)

- Python 3.12+, projeto gerenciado por `uv` (`pyproject.toml` + `uv.lock` versionados).
- Google ADK série 2, versão **exata** fixada: **2.9.2** (D1 resolvida).
- Modelos Gemini com chave do Google AI Studio (varia por projeto; dezenas de
  chamadas no fluxo do avaliador). Decisão D2: `gemini-2.5-flash` nos quatro
  agentes, configurável por variável no `.env`.
- Framework web livre (padrão do curso: FastAPI).
- Armazenamento livre; se externo, sobe com comando documentado.
- Nenhuma chave versionada: `.env` fora do Git, `.env.example` versionado com nomes sem valores.
- `dados/*.json` e `dados/regulamento.md` **nunca** são alterados (avaliador confere identidade no passo 15).

## 3. Estado inicial dos dados (referência para os testes)

- Apartamentos: 101 (Helena Prado), 102 (Bruno Tavares), 201 (Luana Castro), 202 (Otávio Mendes), 301 (Cecília Rocha), 302 (Rafael Moreira).
- Áreas: `salao-de-festas` (taxa 150 — cobra), `churrasqueira` (taxa 80 — cobra), `quadra` (taxa 0 — não cobra).
- Reservas iniciais: RSV-1377 (101/quadra/2030-03-09), RSV-4821 (302/salao/2030-03-16), RSV-2950 (201/churrasqueira/2030-03-23).
- Visitantes iniciais: Marina Duarte (302, 2030-03-16), Paulo Nogueira (201, 2030-03-23).
- Regulamento: `dados/regulamento.md`, 491 linhas, organizado em Capítulos com artigos.

## 4. Arquitetura proposta (esqueleto)

```
src/aurora/
├── __init__.py
├── config.py                 # lê .env (chave, modelo de cada agente, caminhos dos bancos)
├── api/
│   ├── main.py               # FastAPI: rotas do contrato + verificação; Runner por turno
│   └── serializers.py        # eventos do ADK -> JSON (D8)
├── agents/
│   ├── builder.py            # principal + 3 especialistas, App/Runner, anchor_root
│   ├── tools.py              # tools de negócio (apartamento vem da sessão)
│   ├── regulations.py        # índice do regulamento por capítulos + busca (D6)
│   ├── confirmations.py      # lê as confirmações pendentes nos eventos
│   └── fake_llm.py           # modelo fake determinista (sem chave de API)
├── storage/
│   ├── db.py                 # conexão SQLite (WAL, busy_timeout), DDL
│   ├── reservas_store.py     # gravação atômica: UNIQUE parcial (area, data) WHERE ativo = 1
│   ├── visitantes_store.py
│   ├── sessions_store.py     # sessão ADK -> apartamento
│   └── confirmations_store.py# pendências (claim atômico pending -> answered)
└── scripts/
    ├── restore.py            # volta reservas/visitantes ao seed e apaga as sessões
    ├── verificar_storage.py
    ├── verificar_agentes.py
    └── verificar_api.py
run_api.py                    # uvicorn em http://localhost:8000
```

### 4.1 Topologia de agentes

- **Agente principal**: recebe a mensagem do morador, identifica a intenção e
  transfere para o especialista certo. Instruções só sobre roteamento e tom —
  **nunca o regulamento** (Garantia 4) e nunca regras de negócio críticas
  (elas vivem nas tools/código).
- **Especialista de Reservas**: tools de reserva/cancelamento/agenda.
- **Especialista de Visitantes**: tools de autorização/lista.
- **Especialista de Regulamento**: única porta de acesso ao regulamento,
  com a tool que devolve só o trecho relevante.

**[RESOLVIDA — D5]** Três especialistas (reservas, visitantes, regulamento),
todos acionados como tool inline do principal (`mode='single_turn'` +
`disallow_transfer_to_parent=True`): é o que torna a retomada da confirmação
determinística, enquanto `transfer_to_agent` se mostrou intermitente no spike.
Vive em `src/aurora/agents/builder.py`.

### 4.2 Armazenamento

- **Sessões ADK** → `DatabaseSessionService` (SQLite) do próprio ADK.
  Sobrevive ao restart da API (Garantia 3) e é o que o README do enunciado
  indica que funciona com confirmação quando a resposta chega ao agente certo.
  ✅ **RESOLVIDA (D3a):** dois bancos separados — sessões do ADK em
  `var/aurora_sessoes.db` (`sqlite+aiosqlite://`, driver async obrigatório) e
  dados de negócio em `var/aurora_dados.db` (ver `src/aurora/config.py`).
- **Dados de negócio** (reservas/visitantes) → SQLite próprio com
  `UNIQUE(area, data)` na tabela de reservas: a exclusividade vale **no
  instante do INSERT**, não numa conferência prévia (Garantia 5). Conflito
  vira erro tratado na tool → resposta normal do agente (sem 500).
  Cancela-se a reserva por `DELETE`; a geração de código novo (`RSV-####`)
  consulta o conjunto de códigos existentes **incluindo cancelados**
  (regra 5), com `UNIQUE(codigo)` como trava final.
  ✅ **RESOLVIDA (D3b): SQLite.** A alternativa JSON+lock foi considerada e
  rejeitada por não dar atomicidade nativa; o índice UNIQUE parcial
  `(area, data) WHERE ativo = 1` garante a exclusividade no INSERT.
- Os arquivos de `dados/` são **somente leitura**: usados como seed no
  restore e nunca escritos em runtime.

### 4.3 Como cada garantia cai no código (proposta)

| Garantia | Mecanismo no código | Arquivo(s) proposto(s) |
|---|---|---|
| 1. Cobrança/acesso só com confirmação | Tool marcada para confirmação; pendência persistida com id próprio; rota `/confirmacoes` injeta a resposta e retoma o Runner; id respondido sai da lista → 409 | `tools/reservas.py`, `tools/visitantes.py`, `confirmacoes.py`, `runner.py` |
| 2. Sessão pertence a um apartamento | `session.state["apartamento"]` gravado na criação; **nenhuma tool aceita parâmetro de apartamento** — a tool lê da sessão via `ToolContext` | `sessions.py`, `runner.py`, todas as tools |
| 3. Nada se perde no reinício | `DatabaseSessionService` + SQLite de negócio; `App` reidrata a sessão pelo `session_id` | `runner.py`, `storage/db.py` |
| 4. Regulamento consultado, não carregado | Tool `consultar_regulamento(tema)` recupera **apenas** o capítulo/artigo relevante; principal sem regulamento nas instruções; nada de regulamento no histórico | `tools/regulamento.py`, `agents/regulamento_agent.py` |
| 5. Dois moradores, uma reserva | `UNIQUE(area, data)` no INSERT + tratamento do conflito como resposta normal | `storage/reservas_store.py` |

## 5. Contrato da API → implementação

| Rota | Comportamento | Observações de implementação |
|---|---|---|
| `POST /sessoes` | 201 + `session_id` | valida apartamento em `apartamentos.json` **[RESOLVIDA — D9a, ver seção 9]**: 400 com detalhe do erro para inexistente |
| `POST /sessoes/{id}/mensagens` | 200 + `resposta` + `confirmacoes_pendentes` | corpo do contrato: `{"texto": "..."}` (aceita `mensagem` como apelido interno); varre os eventos do turno e sincroniza a tabela de pendências; cada pendência no formato do contrato `{id, acao, detalhes}`; `resposta` vazia se parou em confirmação |
| `POST /sessoes/{id}/confirmacoes` | 200 (mesmo formato) / 409 | corpo `{"id": "...", "confirmado": bool}`; valida id pendente da sessão (nunca reaproveitável); injeta FunctionResponse/ToolConfirmation e retoma a execução; id desconhecido ou já respondido → 409 e nada executa |
| `GET /sessoes/{id}/eventos` | 200 lista completa, em ordem | 404 se sessão não existe; serializar eventos ADK para JSON (ver `api/serializers.py`) |
| `GET /apartamentos/{n}/reservas` | verificação, lê direto do store | lista JSON `[{codigo, area, data}]`, sem passar pelo modelo |
| `GET /apartamentos/{n}/visitantes` | verificação, lê direto do store | lista JSON `[{nome, data}]`, sem passar pelo modelo |

**[RESOLVIDA — D9b, D9c (Fase 4); ver seção 9]** Comportamentos livres
(fora de escopo) fixados: mensagem nova com pendência ativa processa o texto
mantendo a pendência intacta (nada executa sem confirmação); duas respostas
simultâneas à mesma confirmação são idempotentes — só a primeira executa, a
segunda recebe 409.

## 6. Mapeamento do fluxo do avaliador → verificação manual

| Passo | Ação do avaliador | O que temos que garantir |
|---|---|---|
| 1 | clone, `.env`, `uv sync`, comandos do README | restore + subida deixam API no ar com dados seed; verificação inicial OK |
| 2 | POST /sessoes 101 | 201 |
| 3 | pedir dados do 302 na sessão do 101 | nem resposta nem **eventos** contêm RSV-4821 / Marina Duarte (tools nunca buscam de outro apto) |
| 4 | cancelar reserva do 302 | nada muda no 302; nada vaza nos eventos |
| 5 | cancelar própria reserva (quadra 2030-03-09) | sem confirmação (taxa 0 e cancelamento é regra 4), RSV-1377 some |
| 6 | reservar quadra 2030-04-06 | sem confirmação (taxa 0); reserva aparece |
| 7/8 | reservar salão 2030-04-20, negar, repetir e aprovar, reenviar mesmo id | pendência com area+data em `detalhes`; negar não grava; aprovar grava **exatamente 1**; reenvio → 409 |
| 9 | confirmar id inexistente; eventos de sessão inexistente | 409 sem efeito; 404 |
| 10 | sessão S2 (101) reservar salão 2030-03-16 (ocupado pelo 302) | não cria; sem vazamento de RSV-4821 / "302" isolado |
| 11 | liberar visitante "já confirmando aqui" | pendência com nome+data; "já confirmei" não libera; só grava após aprovação |
| 12 | pergunta sobre piscina aos domingos | resposta correta via tool de regulamento; eventos sem trechos de outros capítulos; chamadas de tool presentes |
| 13 | restart sem restore | eventos idênticos, novas mensagens funcionam, dados persistem, códigos novos únicos |
| 14 | S3 (101) e S4 (201), salão 2030-05-11, aprovações simultâneas (`&`) | dois 200 + exatamente 1 reserva no total |
| 15 | inspeção do repo | ADK fixado; `dados/` idênticos; sem chaves; 1 principal + ≥2 especialistas; tools; apto da sessão; exclusividade no INSERT; Garantias do README apontando arquivos reais |

## 7. Fases de execução

### Fase 0 — Setup do repositório
1. Fork público do repositório base; clone; `git remote add upstream`.
2. `uv init` + `pyproject.toml` com `google-adk == <versão>` **[D1]** e FastAPI/uvicorn.
3. `.gitignore` (`.env`, `*.db`, caches), `.env.example` (**GEMINI_API_KEY** + nomes de variáveis de modelo/caminhos).
4. Conferir: `uv sync` roda limpo; `dados/` intocado; `git status` sem lixo.

### Fase 1 — SPIKE: confirmação + persistência (risco nº 1 do enunciado)
> O enunciado avisa: a retomada só funciona se a resposta chegar ao agente que
> pediu a confirmação, e a escolha do Runner muda com topologia, bloqueios de
> transferência, configuração de retomada e serviço de sessão. **Testar com
> sessão persistida e após restart da API** — não só no adk web.
1. Ler: `adk.dev/tools-custom/confirmation/` (métodos `require_confirmation` vs
   `tool_context.request_confirmation`), sample `human_tool_confirmation` do
   adk-python e a fonte do ADK sobre resposta a confirmação pendente.
2. Montar um protótipo mínimo (1 agente, 1 tool com confirmação, session
   service SQLite) e validar: mensagem → pendência → rota responde → tool
   executa 1x → reinício da API → mesma pendência ainda responde.
3. Repetir com 2 agentes + transferência para reproduzir o problema citado.
4. Registrar o mecanismo vencedor (e versão do ADK usada) — decide **D1, D5 e D7**.

### Fase 2 — Storage de negócio
1. DDL SQLite: `reservas(codigo PK, apartamento, area, data, UNIQUE(area,data))`,
   `visitantes(id, apartamento, nome, data)`, `confirmacoes(id, sessao, acao, detalhes_json, status, criado_em)`.
2. Geração de códigos únicos (inclui cancelados).
3. `restore.py`: apaga e recarrega reservas/visitantes do seed; decide se apaga sessões **[D4]**.
4. Teste de concorrência: 2 threads/curl disputando o mesmo slot → 1 grava, 1 recebe recusa normal.

### Fase 3 — Agentes e tools
1. Tools de reservas/visitantes **sem parâmetro de apartamento** (lê da sessão).
2. Tool de consulta de agenda → só "livre/ocupada" (nunca de quem é).
3. Tool de regulamento por capítulo/tema **[D6]**.
4. Especialistas + principal; escolha de modelos **[D2]**.

### Fase 4 — API FastAPI (todas as rotas do contrato + verificação)
1. Sessões: criação com vínculo de apartamento.
2. Mensagens: `App.run` + sincronização de pendências.
3. Confirmações: validação 409 + retomada.
4. Eventos: serialização **[D8]**; 404 para sessão inexistente.

### Fase 5 — Execução do fluxo do avaliador (passo a passo via curl)
- Rodar os 15 passos como checklist; conferir também os eventos (vazamentos).
- Passo 14 com `curl ... & curl ...` simultâneos.
- Passo 13 completo: Ctrl+C, subir de novo, conferir contagem de eventos.

### Fase 6 — README final e entrega
- README com as 3 seções obrigatórias; Garantias apontando **arquivos e trechos reais**.
- Rodar checklist completo dos "Critérios de aceite" do enunciado.
- Commit em `main` do fork público; documentar comandos `restore` e subida.

## 8. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|---|---|
| Retomada de confirmação falha com sessão persistida (pior armadilha do enunciado) | Garantia 1 cai | Spike na Fase 1 com SQLite + restart; testar topologia final |
| Mudança de comportamento entre versões do ADK | Falha imprevisível | Fixar versão exata em `pyproject.toml`; decidir cedo **[D1]**; nunca "latest" solto |
| Quota/limites do Gemini no projeto do aluno | 429 no meio do fluxo | Modelo flash/barato, instruções curtas, dezenas de chamadas só; **[D2]** |
| Vazamento via eventos (apto alheio / capítulos de regulamento) | Passos 3, 4, 10, 12 falham | Design de tools que só retornam o permitido; conferir `GET /eventos` em cada passo |
| Deadlock/lock do SQLite na disputa | Passo 14 falha | WAL + `busy_timeout`; conflito tratado como resposta normal |
| `dados/` alterado por engano | Passo 15 falha | Nenhum código escreve em `dados/`; seed só em memória/SQLite |
| Confirmação executar 2x (reenvio/re-execução) | Passos 8, 9 falham | Pendência sai da lista no primeiro uso; 409 depois; UNIQUE como trava final |

## 9. Pontos de decisão em aberto (CONSOLIDADO)

| # | Decisão | Opções | Recomendação | Impacto | Resolver antes de |
|---|---|---|---|---|---|
| **D1** | Versão exata do ADK a fixar | 2.2.0 (curso) / 2.9.1 (testada pelos autores) / 2.9.2 (mais alta estável) | ✅ **RESOLVIDA (spike): 2.9.2 fixada e validada** — confirmação + sessão persistida SQLite funcionam com topologia single_turn | Alto | Fase 0/1 |
| **D2** | Modelo Gemini de cada agente | flash vs pro; por agente ou um só | ✅ **RESOLVIDA: `gemini-2.5-flash` nos quatro agentes** (variável por agente no `.env`, validada na construção do app) | Médio — quota e custo; dezenas de chamadas | Fase 1/3 |
| **D3a** | Layout do armazenamento | 1 SQLite único (sessões+dados+pendências) vs 2 bancos separados | ✅ **RESOLVIDA: dois bancos separados** (sessões ADK `/ dados de negócio) para isolar o schema do ADK das nossas tabelas; sessões com `sqlite+aiosqlite://` (driver async obrigatório) | Baixo | Fase 1/2 |
| **D3b** | Tecnologia dos dados de negócio | SQLite (UNIQUE) vs JSON+lock | ✅ **RESOLVIDA: SQLite** — atomicidade nativa na gravação (Garantia 5); JSON rejeitado | Alto (Garantia 5) | Fase 2 |
| **D4** | Restore apaga sessões? | Apagar junto / só dados | ✅ **RESOLVIDA (Fase 2): apaga também as sessões** (estado 100% inicial, documentado no comando). `uv run python -m aurora.scripts.restore` recarga reservas/visitantes dos seeds e apaga `var/aurora_sessoes.db` | Baixo | Fase 2 |
| **D5** | Topologia de especialistas | transfer_to_agent vs single_turn | ✅ **RESOLVIDA (spike): especialistas com `mode='single_turn'`** (tool inline do principal) — a retomada de confirmação é determinística (10/10), enquanto transfer_to_agent é flaky (5/8, com no-op silencioso e `cannot transfer to itself`). Nº de especialistas: 3 (reservas, visitantes, regulamento) | Alto (Garantia 1 + 3) | Fase 1/3 |
| **D6** | Recuperação do regulamento | Mapa capítulo→palavras-chave vs embeddings/RAG | ✅ **RESOLVIDA (Fase 3): mapa por capítulos com scoring por tokens** (`agents/regulations.py`, sem dependências; responde os 2 capítulos mais relevantes). Embeddings só se a precisão falhar no passo 12 | Médio (Garantia 4) | Fase 3 |
| **D7** | Mecanismo de confirmação | `require_confirmation=True` (bool simples) vs `tool_context.request_confirmation` (payload/avançado) | ✅ **RESOLVIDA (spike)**: avançado com payload para manter `detalhes`; booleano como opção. Resposta = FunctionResponse para o FC `adk_request_confirmation` (id do FC long-running, extraído dos eventos; `requested_tool_confirmations` é chaveado pelo id da tool original) enviada como `new_message` no `run_async` — sem `invocation_id` explícito (ignorado quando há FR) | Alto (Garantia 1) | Fase 1 (spike) |
| **D8** | Serialização do `GET /eventos` | Converter schema ADK p/ JSON cru vs projeção enxuta | ✅ **RESOLVIDA (Fase 4): projeção fiel e completa** (`api/serializers.py`: id, author, timestamp, end_of_turn, texts, function_calls, function_responses, requested_tool_confirmations, long_running_tool_ids) — os eventos trazem as chamadas de tool do passo 12 sem vazar dados de outro apartamento | Médio — afeta vazamentos e a contagem do passo 13 | Fase 4 |
| **D9a** | Apartamento inexistente no `POST /sessoes` | 400 / 404 / criar mesmo assim | ✅ **RESOLVIDA (Fase 4): 400 com detalhe do erro** — comportamento livre, escolhido pela clareza de depuração | Baixo | Fase 4 |
| **D9b** | Mensagem nova com pendência ativa | Bloquear / processar mantendo pendência | ✅ **RESOLVIDA (Fase 4): processa a mensagem mantendo a pendência intacta**; nada executa sem confirmação | Baixo | Fase 4 |
| **D9c** | Duas respostas simultâneas à mesma confirmação | Livre, sem dupla execução | ✅ **RESOLVIDA (Fase 4): idempotência no nível da pendência** — tabela `confirmations` como fonte de verdade + claim atômico (UPDATE condicional pending→answered); a segunda resposta recebe 409 e não executa nada | Médio | Fase 4 |
| **D9d** | Runner novo por turno (concorrência do passo 14) | Runner global único / Runner novo por rota | ✅ **RESOLVIDA (Fase 4): cada rota de mensagem/confirmação cria um Runner novo** (mesmo banco de sessões SQLite); escrituras concorrentes em sessões diferentes não se pisam e o restart é fiel à Garantia 3 | Médio (passo 14) | Fase 4 |

Decisões que **não** estão em aberto (já tomadas pelo enunciado ou por
recomendação forte): contrato REST fixo; FastAPI; SQLite como storage;
ferramentas de dados de negócio sem parâmetro de apartamento; `dados/` apenas
leitura; exclusividade via índice UNIQUE PARCIAL `(area, data) WHERE ativo=1`
no INSERT — nota de design: como os cancelados conservam a fila (códigos não
reutilizáveis), um UNIQUE global bloquearia re-reservar; o índice parcial
mantém a exclusividade somente entre ativas (validado 18/18 na Fase 2).

**Achado Fase 3 (ADK 2.9.2, topologia single_turn + confirmação)**: depois
de RESOLVER uma confirmação, a sessão continua a invocação do sub-agente — os
próximos textos do morador entram NO MESMO especialista (o root é cancelado).
Na época isso não impedia o fluxo do enunciado; na Fase 4 ficou claro que
prejudica qualquer sessão que misture domínios (a S1 do avaliador mistura
reservas, visitante e regulamento), e o mecanismo da Fase 4 abaixo resolve.
Documentado em `spikes/001-confirmacao-persistida/README.md`; verificado 14/14.

**Achado Fase 4 (ADK 2.9.2, topologia single_turn)**: depois de RESOLVER
uma confirmação, o último evento da sessão é do especialista e o
`_agent_router` pode re-eleger o especialista para os próximos textos,
prendendo a sessão no último domínio usado (a S1 do avaliador mistura
reservas, visitante e regulamento). Mecanismo vencedor, verificado em
`agents/builder.py`:

1. `disallow_transfer_to_parent=True` nos três especialistas: o router só
   mantém um sub-agente no comando do turno seguinte se ele for
   "transferível" para a árvore inteira (`is_transferable_across_agent_tree`
   exige a flag `False` no agente e em todos os ancestrais). Com a flag, os
   eventos dos especialistas são ignorados na varredura e a resposta cai no
   principal de forma DETERMINÍSTICA. Sem ela a falha era intermitente
   (~4/20 em `verificar_agentes`): a varredura depende da ordem dos eventos
   e o `DatabaseSessionService` ordena por `(timestamp DESC, id DESC)` com
   id UUID aleatório, então eventos empatados saem em ordem arbitrária.
2. `anchor_root()` (fim de cada turno resolvido) anexa um evento sintético
   `Event(author='main_agent')` com timestamp estritamente maior que o maior
   já gravado (`max(timestamps) + 1e-3`), garantindo que o último evento do
   turno seja do principal. Um turno que terminou pedindo confirmação NÃO é
   ancorado: o último evento precisa continuar sendo o
   `adk_request_confirmation` do especialista.

A retomada de confirmações não depende de qual agente o router escolha: ela
vem do `FunctionResponse` da confirmação pendente
(`find_matching_function_call`), não da varredura de eventos (verificado
10/10 no spike 002). Resultados após o fix, com execução real:
`verificar_api` 20/20 + 12/12 (31 checks, 0 falhas), `verificar_agentes`
10/10 (23 checks) e o cenário de mistura de domínios 40/40.

**Verificação Fase 5 (fluxo do avaliador, modelo fake)**: os passos 1-14 foram
executados contra o `uvicorn` real (HTTP em `localhost:8000`, sem transporte
ASGI), com o modelo fake determinista, incluindo o passo 13 (Ctrl+C, subir de
novo) e o passo 14 com as duas aprovações simultâneas em
`curl ... & curl ... --wait`. Resultado: 1-12 com 30 checks e 0 falhas; passo 13
com 80 eventos idênticos antes/depois do restart, novas mensagens funcionando e
códigos novos sem colisão; passo 14 com dois `200` e exatamente 1 reserva do
salão em 2030-05-11 (a outra resposta venceu com um erro de negócio normal,
`date_taken`, sem 500).

**Verificação com Gemini real (`gemini-2.5-flash`, Google AI Studio)**: os
passos 1-14 foram repetidos contra o `uvicorn` real com o LLM de verdade —
38 checks nos passos 1-12 e 18 nos passos 13-14 (reinício real da API sem
restore e as duas aprovações simultâneas), 0 falhas. Quatro defeitos apareceram
só aqui, nenhum visível no modelo fake: id da área em linguagem natural
("Salão de festas" em vez de `salao-de-festas`), cancelamento por descrição (o
modelo pedia o código ao morador), vazamento do Capítulo II numa consulta sobre
a piscina (score sem IDF e sem corte sobre o melhor) e sessão presa no
especialista quando uma mensagem de texto chegava com confirmação pendente
(re-ancoragem no principal antes do turno). Detalhes no README (seção
"Validação") e no MR #5.

Documentado em `spikes/002-transfer-topology/README.md`.

## 10. Pendências de pesquisa (links oficiais)

- Confirmação de ações (ADK): `https://adk.dev/tools-custom/confirmation/`
- Sample de confirmação com humano: `github.com/google/adk-python/blob/main/contributing/samples/human_tool_confirmation/agent.py`
- Sessões persistidas / `DatabaseSessionService`: docs de session service do ADK (adk.dev/sessions/…)
- `App.run`/retomada e eventos: docs de execution/runtime do ADK + fonte `google/adk-python`
- Modelos Gemini disponíveis e limites: Google AI Studio (conta do aluno)