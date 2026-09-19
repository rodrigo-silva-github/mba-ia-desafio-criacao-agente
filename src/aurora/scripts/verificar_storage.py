"""Verificação da Fase 2 (storage de negócio) — roda sem chave de API.

Uso:  uv run python -m aurora.scripts.verificar_storage

Cubre: restauração do estado inicial, CRUD de reservas e visitantes,
unicidade de códigos (incluidos os cancelados), exclusividade área+data no
instante da gravação (Garantia 5) e escrituras em paralelo.
"""

from __future__ import annotations

import asyncio
import os
import sys

from .. import config
from ..storage import db, reservas_store, visitantes_store

PASS = 0
FAIL = 0


def check(condicao: bool, rotulo: str) -> None:
  global PASS, FAIL  # noqa: PLW0603
  if condicao:
    PASS += 1
    print(f"  [ok] {rotulo}")
  else:
    FAIL += 1
    print(f"  [FALHOU] {rotulo}")


async def cenario_seed() -> None:
  print("\n== Seeds (após restore) ==")
  from ..scripts import restore

  await restore.restaurar()

  reservas_101 = await reservas_store.listar_apartamento("101")
  check(len(reservas_101) == 1 and reservas_101[0]["codigo"] == "RSV-1377", "101 tem só a RSV-1377")
  reservas_302 = await reservas_store.listar_apartamento("302")
  check(len(reservas_302) == 1 and reservas_302[0]["codigo"] == "RSV-4821", "302 tem só a RSV-4821")
  visitantes_302 = await visitantes_store.listar_apartamento("302")
  check(
      len(visitantes_302) == 1 and visitantes_302[0]["nome"] == "Marina Duarte",
      "302 tem Marina Duarte",
  )
  check(await reservas_store.esta_libre("quadra", "2030-03-09") is False, "quadra 2030-03-09 ocupada")
  check(await reservas_store.esta_libre("quadra", "2030-04-06") is True, "quadra 2030-04-06 livre")


async def cenario_crud() -> None:
  print("\n== CRUD ==")
  r = await reservas_store.reservar("101", "quadra", "2030-04-06")
  check(r.ok and r.reserva and r.reserva.codigo.startswith("RSV-"), "reserva nova criada")
  assert r.reserva
  codigo1 = r.reserva.codigo

  r2 = await reservas_store.reservar("201", "quadra", "2030-04-06")
  check(not r2.ok and r2.motivo == "data_ocupada", "dup área+data recusada (Garantia 5)")

  check(await reservas_store.cancelar("101", codigo1) is True, "cancelou a própria reserva")
  check(await reservas_store.cancelar("302", codigo1) is False, "não cancela reserva alheia (G2)")

  r3 = await reservas_store.reservar("101", "quadra", "2030-04-06")
  check(r3.ok and r3.reserva and r3.reserva.codigo != codigo1, "código novo difere do cancelado")
  assert r3.reserva
  lista = [x["codigo"] for x in await reservas_store.listar_apartamento("101")]
  check(codigo1 not in lista, "reserva cancelada não aparece na lista")

  v = await visitantes_store.autorizar("101", "Joana Ribeiro", "2030-04-21")
  check(v["nome"] == "Joana Ribeiro" and v["data"] == "2030-04-21", "visitante autorizado")
  nomes = [x["nome"] for x in await visitantes_store.listar_apartamento("101")]
  check("Joana Ribeiro" in nomes, "visitante listado no 101")


async def cenario_concorrencia() -> None:
  print("\n== Concorrência (Garantia 5) ==")

  # Mesma área+data, duas escritas ao mesmo tempo -> exatamente uma vence.
  async def tenta(apartamento: str) -> dict:
    res = await reservas_store.reservar(apartamento, "salao-de-festas", "2030-05-11")
    return {"ok": res.ok, "codigo": res.reserva.codigo if res.reserva else None}

  resultados = await asyncio.gather(tenta("101"), tenta("201"))
  vencedores = [r for r in resultados if r["ok"]]
  check(len(vencedores) == 1, f"disputa salão 2030-05-11: exatamente 1 vencedor (={len(vencedores)})")

  # Áreas diferentes ao mesmo tempo -> ambas vencem, códigos diferentes.
  async def tenta_b(apartamento: str, area: str) -> dict:
    res = await reservas_store.reservar(apartamento, area, "2030-06-01")
    return {"ok": res.ok, "codigo": res.reserva.codigo if res.reserva else None}

  res_b = await asyncio.gather(tenta_b("101", "salao-de-festas"), tenta_b("201", "churrasqueira"))
  check(all(r["ok"] for r in res_b), "escritas paralelas em slots distintos: ambas ok")
  check(res_b[0]["codigo"] != res_b[1]["codigo"], "códigos diferentes entre as duas")

  # Nenhum código repetido entre as reservas ativas (incluindo criadas no teste).
  todas = await reservas_store.todas_ativas()
  codigos = [r["codigo"] for r in todas]
  check(len(codigos) == len(set(codigos)), "sem códigos repetidos entre ativas")
  check(len(codigos) >= 5, f"total de ativas cresceu ({len(codigos)})")


async def main() -> int:
  # Começa do banco zerado (evita lixo de execuções anteriores).
  for p in (config.BANCO_DADOS, config.BANCO_DADOS + "-wal", config.BANCO_DADOS + "-shm"):
    if os.path.exists(p):
      os.remove(p)
  await db.iniciar()

  await cenario_seed()
  await cenario_crud()
  await cenario_concorrencia()

  print(f"\nRESULTADO: {PASS} ok, {FAIL} falhas")
  return 0 if FAIL == 0 else 1


if __name__ == "__main__":
  sys.exit(asyncio.run(main()))