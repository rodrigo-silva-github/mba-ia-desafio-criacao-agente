# Spike 002 — Re-ancoragem na raiz após resolver uma confirmação (ADK 2.9.2)

## Problema medido

Neste wheel de ADK 2.9.2 (build com o runtime novo: `runners.py`, workflow,
router em `_agent_router.py`), após RESOLVER uma confirmação de tool dentro
de um sub-agente, a sessão fica "presa" naquele especialista:
`_find_agent_to_run` percorre os eventos em ordem inversa e devolve o primeiro
autor conhecido (o especialista), então a próxima mensagem do morador entra
no especialista errado. O defeito é do RUNTIME (independente do modelo) e
acontece com AMBAS as topologias:

| Topologia | Retomada (tool aprovada grava) | Próxima mensagem mista (visitante/piscina) |
|---|---|---|
| single_turn | 10/10 | 0/10 |
| transfer_to_agent | 10/10 | 0/10 |

Como o avaliador mistura domínios DENTRO de S1 (reservas 3-10, visitante 11,
piscina 12), nenhuma topologia serve sozinha.

## Solução

Re-ancoragem: anexa-se à sessão um evento SINTÉTICO com `author=main_agent`
via `session_service.append_event(session, Event(author=main, timestamp=now+0.01))`.
O timestamp no FUTURO é determinante: o armazenamento ordena por
`(timestamp DESC, id DESC)` e o router olha o último evento, então o evento
sintético fica DURADOURAMENTE por último (os eventos reais seguintes têm
`now() < agora+0.01` e são ordenados antes) e o router devolve sempre a raiz.
Resultado: um único evento sintético por sessão (o primeiro já é o último
para sempre).

Achado complementar: a retomada da confirmação (tool aprovada) NÃO depende do
agente escolhido pelo router: com o evento sintético presente (o router
devolve `main` no turno de aprovação), a tool é re-executada igualmente e
grava 10/10.

## Protocolo medido (10 iterações + restart real)

1. Reservar salão nova data -> pending de `book_area`
2. Aprovar (mesmo Runner) -> grava
3. "Libera a Joana Ribeiro el 2030-04-21" -> `visitors_agent` (antes falhava)
4. Aprovar -> grava
5. "¿A qué hora cierra la piscina los domingos?" -> `query_regulations`
6. Restart real: pending criado com Runner A, aprovado com Runner B

Resultados: pendings 10/10 · aprovadas 10/10 · visitante 10/10 · aprovação
visitante 10/10 · piscina 10/10 · retomada pós-restart 3/3.

## Atualização (Fase 4) — mecanismo definitivo na API

O spike acima mediu a re-ancoragem com `timestamp=now+0.01` ANTES do turno.
Na integração da API (Fase 4) esse desenho mostrou-se frágil: o
`DatabaseSessionService` ordena por `(timestamp DESC, id DESC)` e o id é um
UUID aleatório, então eventos com timestamps empatados saem em ordem
arbitrária — a sessão voltava ao especialista em ~4/20 das corridas
(`verificar_agentes`, turno de visitante depois de um turno de regulamento).

O mecanismo definitivo tem DUAS partes (ambas em
`src/aurora/agents/builder.py`):

1. `disallow_transfer_to_parent=True` nos especialistas — o router só mantém
   um sub-agente no comando do turno seguinte se ele for transferível para a
   árvore inteira (`is_transferable_across_agent_tree`); com a flag, os
   eventos dos especialistas são ignorados e o router cai no principal de
   forma determinística.
2. `anchor_root()` ao FIM de cada turno resolvido, com timestamp
   estritamente maior que o maior já gravado (`max(timestamps) + 1e-3`), e
   nunca em um turno que parou em confirmação.

Resultados após o fix (execução real): `verificar_api` 20/20 + 12/12 (31
checks, 0 falhas), `verificar_agentes` 10/10 (23 checks) e o cenário de
domínios mistos (regulamento -> visitante) 40/40.

## Uso

    uv run python spikes/002-transfer-topology/main.py

(usa bancos temporários `var/spike2_*.db`; não toca nos dados do projeto.)

## Arquivos

- `main.py` — protocolo de 10 iterações + restart real.
- `fake_llm.py` — modelo fake determinístico (rotina single_turn) com
  normalização de texto para as chaves de roteamento (acentos: "salão" -> salao).
