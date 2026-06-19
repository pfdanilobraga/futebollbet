# -*- coding: utf-8 -*-
"""
Backtest pré-jogo: calibração, EV e Closing Line Value (CLV).

Consome as previsões out-of-fold do walk-forward (`oof_preds.csv`, gerado por
`treino.py`) e cruza com as odds 1X2 do banco:
  - odd_abertura  -> linha de ABERTURA (preço "cedo", onde se aposta por valor)
  - odd_decimal   -> última linha registrada (proxy de FECHAMENTO)

Mede três coisas, na ordem de importância para ESTE projeto:

  1. CALIBRAÇÃO (estrela-guia): quando o modelo diz X%, acontece ~X%?
     Diagrama de confiabilidade + ECE (Expected Calibration Error), comparado
     ao mercado. É a métrica honesta de precisão de probabilidade.

  2. CLV — Closing Line Value (termômetro de skill): entre as apostas que o
     modelo aponta como valor no preço de ABERTURA, a linha se moveu a favor
     até o FECHAMENTO? CLV médio positivo é o sinal mais confiável de que o
     modelo "antecipa o mercado". RESSALVA: odds bet365 são soft (não Pinnacle
     no-vig) e o "fechamento" é a última linha registrada -> sugestivo, não prova.

  3. EV / ROI hipotético: apostando 1u nas seleções com EV>limiar no preço de
     abertura. ROI aqui é otimista (não se consegue sempre a linha de abertura);
     serve de sanity-check, NÃO de promessa de lucro.

Uso:
  py backtest.py                 # usa oof_preds.csv
  py backtest.py --ev 0.05       # limiar de EV p/ marcar aposta de valor (5%)
"""
import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
OOF_PATH = os.path.join(PASTA, "oof_preds.csv")
CLASSES = ["H", "D", "A"]              # ordem = colunas p_casa/p_empate/p_fora
ESCOLHA = {0: "1", 1: "X", 2: "2"}     # índice -> escolha no mercado 1X2


# ----------------------------------------------------------------- odds
def carregar_odds(con):
    """evento_id -> {'dec': (o1,oX,o2), 'abe': (a1,aX,a2)} para Full time 1X2
    completo (os três preços presentes)."""
    linhas = con.execute(
        """SELECT evento_id, escolha, odd_decimal, odd_abertura FROM odd
           WHERE mercado='Full time' AND parametro='' AND escolha IN ('1','X','2')
             AND odd_decimal > 1""").fetchall()
    tmp = {}
    for eid, esc, dec, abe in linhas:
        d = tmp.setdefault(eid, {})
        d[esc] = (dec, abe if abe and abe > 1 else dec)
    out = {}
    for eid, d in tmp.items():
        if all(k in d for k in ("1", "X", "2")):
            out[eid] = {
                "dec": (d["1"][0], d["X"][0], d["2"][0]),
                "abe": (d["1"][1], d["X"][1], d["2"][1]),
            }
    return out


def no_vig(odds):
    """odds decimais (o1,oX,o2) -> probabilidade implícita sem margem (soma 1)."""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


# ----------------------------------------------------------------- calibração
def reliability(pred, hit, n_bins=10):
    """Diagrama de confiabilidade + ECE.
    pred: probabilidades previstas (achatadas sobre as 3 saídas).
    hit:  1/0 se aquela saída ocorreu.
    Retorna (linhas_tabela, ece)."""
    pred = np.asarray(pred); hit = np.asarray(hit)
    bordas = np.linspace(0, 1, n_bins + 1)
    linhas, ece, n = [], 0.0, len(pred)
    for i in range(n_bins):
        lo, hi = bordas[i], bordas[i + 1]
        m = (pred >= lo) & (pred < hi if i < n_bins - 1 else pred <= hi)
        if not m.any():
            continue
        conf, acc, cnt = pred[m].mean(), hit[m].mean(), m.sum()
        ece += abs(acc - conf) * cnt / n
        linhas.append((lo, hi, cnt, conf, acc))
    return linhas, ece


def achatar(df, pcols):
    """Devolve (pred, hit) achatados sobre as 3 saídas de cada jogo."""
    pred, hit = [], []
    for _, r in df.iterrows():
        for i, pc in enumerate(pcols):
            pred.append(r[pc])
            hit.append(1.0 if r["alvo"] == CLASSES[i] else 0.0)
    return np.array(pred), np.array(hit)


def imprimir_reliability(titulo, pred, hit):
    linhas, ece = reliability(pred, hit)
    print(f"\n{titulo}  (ECE={ece:.4f})")
    print("    faixa        n   prev   real")
    for lo, hi, cnt, conf, acc in linhas:
        print(f"    {lo:.1f}-{hi:.1f}  {cnt:>5}  {conf:5.1%}  {acc:5.1%}")
    return ece


# ----------------------------------------------------------------- main
def backtest(ev_limiar=0.0):
    if not os.path.exists(OOF_PATH):
        print(f"Faltando {OOF_PATH}. Rode `py treino.py` primeiro.")
        return
    oof = pd.read_csv(OOF_PATH)
    con = sqlite3.connect(DB)
    try:
        odds = carregar_odds(con)
    finally:
        con.close()

    pcols = ["p_casa", "p_empate", "p_fora"]
    com = oof[oof["evento_id"].isin(odds.keys())].reset_index(drop=True)
    print(f"OOF: {len(oof)} jogos | com odds 1X2 completas: {len(com)} "
          f"({len(com)/max(1,len(oof)):.0%})")

    # ---------- 1. CALIBRAÇÃO ----------
    print("\n" + "=" * 60 + "\n1) CALIBRAÇÃO (estrela-guia)\n" + "=" * 60)
    pm, hm = achatar(oof, pcols)
    imprimir_reliability("Modelo — todos os jogos OOF", pm, hm)
    if len(com):
        pmc, hmc = achatar(com, pcols)
        imprimir_reliability("Modelo — só jogos com odds", pmc, hmc)
        # mercado (no-vig do fechamento) como referência
        pmk, hmk = [], []
        for _, r in com.iterrows():
            mk = no_vig(odds[r["evento_id"]]["dec"])
            for i in range(3):
                pmk.append(mk[i]); hmk.append(1.0 if r["alvo"] == CLASSES[i] else 0.0)
        imprimir_reliability("Mercado (no-vig fechamento) — referência", pmk, hmk)

    if not len(com):
        print("\nSem jogos com odds para EV/CLV.")
        return

    # ---------- 2 & 3. EV / CLV / ROI ----------
    print("\n" + "=" * 60 + "\n2) VALOR (EV no preço de abertura) + 3) CLV\n" + "=" * 60)
    registros = []
    for _, r in com.iterrows():
        o = odds[r["evento_id"]]
        p = [r[c] for c in pcols]
        for i in range(3):
            abe, dec = o["abe"][i], o["dec"][i]
            ev = p[i] * abe - 1.0                 # EV apostando na abertura
            registros.append({
                "temporada_id": r["temporada_id"],
                "saida": i,
                "ev": ev,
                "abe": abe, "dec": dec,
                "clv": abe / dec - 1.0,            # >0 = abertura melhor que fechamento
                "ganhou": 1.0 if r["alvo"] == CLASSES[i] else 0.0,
            })
    reg = pd.DataFrame(registros)
    apostas = reg[reg["ev"] > ev_limiar]
    print(f"\nApostas de valor (EV>{ev_limiar:.0%} na abertura): {len(apostas)} "
          f"de {len(reg)} candidatas ({len(com)} jogos × 3 saídas)")
    if len(apostas):
        roi = (apostas["ganhou"] * (apostas["abe"] - 1) - (1 - apostas["ganhou"])).mean()
        clv = apostas["clv"].mean()
        clv_pos = (apostas["clv"] > 0).mean()
        print(f"  acerto:        {apostas['ganhou'].mean():.1%}")
        print(f"  ROI (abertura, hipotético, OTIMISTA): {roi:+.1%}")
        print(f"  CLV médio (abertura→fechamento):      {clv:+.2%}  "
              f"| linha moveu a favor em {clv_pos:.0%} das apostas")
        print("  [ressalva] odds soft (bet365); CLV é sugestivo de skill, não prova de lucro.")

    # CLV também sobre a MELHOR aposta de cada jogo (maior EV), independente de limiar
    melhores = reg.loc[reg.groupby(reg.index // 3)["ev"].idxmax()]
    print(f"\nCLV da melhor aposta por jogo (n={len(melhores)}): "
          f"{melhores['clv'].mean():+.2%}  | a favor em {(melhores['clv']>0).mean():.0%}")

    # por liga
    print("\n  -- CLV das apostas de valor por liga --")
    for tid, sub in apostas.groupby("temporada_id"):
        print(f"    temp {int(tid):>6} (n={len(sub):>3}):  CLV={sub['clv'].mean():+.2%}"
              f"  ROI={((sub['ganhou']*(sub['abe']-1))-(1-sub['ganhou'])).mean():+.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ev", type=float, default=0.0, help="limiar de EV (ex.: 0.05)")
    a = ap.parse_args()
    backtest(a.ev)
