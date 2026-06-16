# -*- coding: utf-8 -*-
"""
validar_mercado.py — backtest dos mercados de CONTAGEM (escanteios, chutes...).
Liquida as probabilidades gravadas em `probabilidade_mercado` contra o TOTAL
real do jogo (de `evento_estatistica`) e mede:
  - Brier score e log-loss (qualidade da probabilidade), por mercado+modelo
  - P&L / ROI apostando flat no lado favorito do modelo, quando há odds na tela
  - diagrama de confiabilidade (prob prevista vs frequência real)

Análogo do bet365/validar_sinais.py, mas para Over/Under (não 1X2) e sobre
jogos JÁ FINALIZADOS (backtest), não sinais ao vivo.

Só faz sentido depois de:
  py mercados.py proximos   (ou evento) — grava as probabilidades
  ...os jogos terminarem e os detalhes serem coletados (evento_estatistica).

Uso:
  py validar_mercado.py                          # todos os mercados, modelo 'combinado'
  py validar_mercado.py --mercado escanteios --modelo poisson
  py validar_mercado.py --modelo odds_implicitas --stake 1
"""
import argparse
import math
import os
import sqlite3
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")

import mercados as MK


def total_real(con, evento_id, nomes):
    """Total real (casa+fora) da estatística no jogo, ou None."""
    ph = ",".join("?" * len(nomes))
    row = con.execute(
        f"""SELECT casa_valor + fora_valor FROM evento_estatistica
            WHERE evento_id=? AND periodo='ALL' AND nome IN ({ph})
              AND casa_valor IS NOT NULL LIMIT 1""",
        (evento_id, *nomes)).fetchone()
    return row[0] if row else None


def odd_lado(con, evento_id, cfg, linha, lado):
    """Odd decimal do lado (over/under) na linha, da tabela `odd`, ou None."""
    cond = " OR ".join("LOWER(mercado) LIKE ?" for _ in cfg["odds"])
    args = [f"%{t.lower()}%" for t in cfg["odds"]]
    for parametro, escolha, odd in con.execute(
            f"""SELECT parametro, escolha, odd_decimal FROM odd
                WHERE evento_id=? AND ({cond})""", (evento_id, *args)):
        try:
            if abs(float(str(parametro).replace(",", ".")) - linha) > 1e-9:
                continue
        except (TypeError, ValueError):
            continue
        e = (escolha or "").strip().lower()
        l = "over" if e in ("over", "mais", "acima") else (
            "under" if e in ("under", "menos", "abaixo") else None)
        if l == lado and odd and odd > 1:
            return odd
    return None


def desfecho(total, linha):
    """'over' | 'under' | 'push' (linha inteira batida exata)."""
    if abs(total - linha) < 1e-9:
        return "push"
    return "over" if total > linha else "under"


def validar(con, mercados, modelo, stake):
    for mercado in mercados:
        cfg = MK.MERCADOS[mercado]
        rows = con.execute(
            """SELECT pm.evento_id, pm.linha, pm.p_over, pm.p_under
               FROM probabilidade_mercado pm
               JOIN evento e ON e.id = pm.evento_id
               WHERE pm.mercado=? AND pm.modelo=? AND e.status='finished'
               ORDER BY e.inicio_ts""",
            (mercado, modelo)).fetchall()
        amostras = []
        for eid, linha, p_over, p_under in rows:
            total = total_real(con, eid, cfg["stat"])
            if total is None:
                continue
            d = desfecho(total, linha)
            if d == "push":
                continue
            y_over = 1 if d == "over" else 0
            # P&L: aposta no lado favorito do modelo se houver odd e tiver valor
            fav = "over" if p_over >= p_under else "under"
            p_fav = p_over if fav == "over" else p_under
            odd = odd_lado(con, eid, cfg, linha, fav)
            valor = odd is not None and p_fav > 0 and odd > 1.0 / p_fav
            pnl = None
            if valor:
                ganhou = (fav == d)
                pnl = (odd - 1.0) * stake if ganhou else -stake
            amostras.append((p_over, y_over, p_fav, fav, pnl))

        print(f"\n=== {mercado}  (modelo {modelo}) ===")
        if not amostras:
            print("  sem amostras liquidáveis (faltam probabilidades, odds ou totais reais).")
            continue
        n = len(amostras)
        brier = sum((p - y) ** 2 for p, y, *_ in amostras) / n
        ll = -sum(
            (y * math.log(max(p, 1e-9)) + (1 - y) * math.log(max(1 - p, 1e-9)))
            for p, y, *_ in amostras) / n
        freq_over = sum(y for _, y, *_ in amostras) / n
        prob_over_media = sum(p for p, *_ in amostras) / n
        print(f"  {n} jogos  |  Over real {freq_over*100:.1f}%  (modelo previa "
              f"{prob_over_media*100:.1f}%)  |  Brier {brier:.4f}  log-loss {ll:.4f}")

        apostas = [a for a in amostras if a[4] is not None]
        if apostas:
            pnl = sum(a[4] for a in apostas)
            acertos = sum(1 for a in apostas if a[4] > 0)
            print(f"  P&L (apostas de valor, flat {stake}u): {len(apostas)} apostas, "
                  f"{acertos} verdes  |  {pnl:+.2f}u  |  ROI {pnl/len(apostas)/stake*100:+.1f}%")
        else:
            print("  P&L: sem odds na tela para apostar (colete odds dos mercados).")

        # confiabilidade do lado OVER
        print("  --- confiabilidade (prob Over prevista -> Over real) ---")
        for lo, hi in [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]:
            sub = [a for a in amostras if lo <= a[0] < hi]
            if sub:
                real = sum(a[1] for a in sub) / len(sub)
                print(f"    {lo*100:3.0f}-{hi*100:3.0f}%: n={len(sub):3}  Over real {real*100:5.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mercado", choices=list(MK.MERCADOS), default=None)
    ap.add_argument("--modelo", default="combinado",
                    choices=["odds_implicitas", "poisson", "combinado"])
    ap.add_argument("--stake", type=float, default=1.0)
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    try:
        mercados = [a.mercado] if a.mercado else list(MK.MERCADOS)
        validar(con, mercados, a.modelo, a.stake)
    finally:
        con.close()


if __name__ == "__main__":
    main()
