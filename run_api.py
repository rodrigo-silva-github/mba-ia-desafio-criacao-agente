"""Inicialização da API do Residencial Aurora.

Uso:  uv run python run_api.py   (servidor em http://localhost:8000)
Ver README.md -> Fase 4 (API).
"""

import uvicorn

from aurora.api import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)