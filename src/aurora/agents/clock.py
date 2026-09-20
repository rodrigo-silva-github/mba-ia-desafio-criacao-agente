"""Relógio do ADK: garante timestamp único por evento (sem empate).

Por que existe: o `DatabaseSessionService` lê os eventos com
`ORDER BY timestamp DESC, id DESC` e o `id` é um UUID aleatório. Dois eventos
com o MESMO timestamp saem em ordem arbitrária a cada leitura — e quando o
empate cai entre a mensagem do morador e o primeiro turno do modelo, a ordem
pode inverter (function call ANTES da mensagem). O Gemini então recusa o
pedido inteiro:

    400 INVALID_ARGUMENT: Please ensure that function call turn comes
    immediately after a user turn or after a function response turn.

e a API devolvia 500 ao morador (visto no passo 4 do fluxo do avaliador, com o
modelo real; com o modelo fake o defeito é silencioso, porque o fake não valida
a estrutura do pedido).

O ADK carimba cada `Event` com `platform_time.get_time()` no momento da
criação (`google/adk/events/event.py`). Aqui instalamos um provedor que NUNCA
repete um valor: se o relógio não avançou desde o último evento, avança 0,1 ms.
Sem empate, a ordem lida é sempre a ordem de criação, que é a ordem válida.
"""

from __future__ import annotations

import threading
import time

from google.adk.platform import time as adk_time

_PASSO = 1e-4  # 0,1 ms: só separa eventos criados no mesmo instante
_lock = threading.Lock()
_ultimo = 0.0


def _relogio_unico() -> float:
    global _ultimo
    with _lock:
        agora = time.time()
        if agora <= _ultimo:
            agora = _ultimo + _PASSO
        _ultimo = agora
        return agora


def install() -> None:
    """Instala o relógio único no contexto atual (idempotente, barato)."""
    adk_time.set_time_provider(_relogio_unico)
