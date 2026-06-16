# -*- coding: utf-8 -*-
"""
treino_mercado.py — ML para mercados de CONTAGEM (escanteios, chutes, ...).
Prevê o TOTAL esperado da estatística e converte em P(Over/Under) por linha.

Por que regressão (e não classificação direta de uma linha): o total prevê
QUALQUER linha (8.5, 9.5, 10.5...) de uma vez, via Poisson(média prevista).
Mais geral e estável com pouca amostra que treinar um classificador por linha.

- Split CRONOLÓGICO (treina no passado, testa no futuro) — sem vazamento.
- Modelo: PoissonRegressor (regressão linear apropriada para contagem).
- Conversão média->P(Over) via Poisson (reusa mercados.over_under).
- Avaliação: MAE do total + Brier/log-loss de Over/Under (média das linhas),
  comparados a baselines:
    * média do treino (preditor ingênuo do total)
    * Poisson força ataque/defesa (a feature lam_poisson — o modelo atual)
- Salva o regressor em `modelo_<mercado>.joblib`.

Uso:
  py treino_mercado.py                       # escanteios
  py treino_mercado.py --mercado chutes --teste 0.2
"""
import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import joblib
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import PoissonRegressor

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import features_mercado as FM
import mercados as MK

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
MIN_TREINO = 50


# ----------------------------------------------------------------- métricas O/U
def metricas_ou(medias, totais, linhas):
    """Brier e log-loss médios de Over/Under, sobre as linhas dadas, dado um
    vetor de MÉDIAS previstas (convertidas em P(Over) por Poisson) e os totais
    reais. Ignora push (linha inteira batida exata)."""
    brier = ll = 0.0
    n = 0
    for mu, total in zip(medias, totais):
        for linha in linhas:
            if abs(total - linha) < 1e-9:        # push
                continue
            p_over, _ = MK.over_under(max(mu, 1e-6), linha)
            y = 1 if total > linha else 0
            p = min(max(p_over, 1e-9), 1 - 1e-9)
            brier += (p - y) ** 2
            ll += -(y * np.log(p) + (1 - y) * np.log(1 - p))
            n += 1
    return (brier / n, ll / n) if n else (float("nan"), float("nan"))


def carregar_df(mercado):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        linhas = FM.construir_dataset(con, mercado)
    finally:
        con.close()
    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    return df.sort_values(["inicio_ts", "evento_id"]).reset_index(drop=True)


def treinar(mercado, frac_teste=0.2):
    df = carregar_df(mercado)
    n = len(df)
    if n == 0:
        print(f"Sem jogos com a estatística '{mercado}'. Rode: py coletor.py pendentes")
        return
    linhas_mkt = MK.MERCADOS[mercado]["linhas"]
    print(f"{n} jogos com '{mercado}'.  Total médio: {df['total'].mean():.2f}  "
          f"(linhas avaliadas: {linhas_mkt})")
    if n < MIN_TREINO:
        print(f"\n⚠️  Apenas {n} jogos — insuficiente p/ modelo confiável "
              f"(ideal 1000+). Treinando para validar o pipeline; números de referência.")

    cols = [c for c in FM.COLUNAS if df[c].notna().any()]
    vazias = [c for c in FM.COLUNAS if c not in cols]
    if vazias:
        print(f"Features ignoradas (sem dados): {', '.join(vazias)}")
    X = df[cols].astype(float)
    y = df["total"].astype(float).values

    corte = int(n * (1 - frac_teste))
    Xtr, Xte = X.iloc[:corte], X.iloc[corte:]
    ytr, yte = y[:corte], y[corte:]
    tot_te = yte.tolist()
    print(f"\nSplit cronológico: treino={len(Xtr)}  teste={len(Xte)}\n")

    # ---- baselines ----
    print("BASELINES (teste):")
    media_tr = float(np.mean(ytr))
    mae_base = float(np.mean(np.abs(yte - media_tr)))
    b, l = metricas_ou([media_tr] * len(yte), tot_te, linhas_mkt)
    print(f"  média do treino ({media_tr:.2f})   MAE={mae_base:.3f}  brierOU={b:.4f}  loglossOU={l:.4f}")
    if "lam_poisson" in cols and Xte["lam_poisson"].notna().any():
        lam = Xte["lam_poisson"].fillna(media_tr).values
        mae_p = float(np.mean(np.abs(yte - lam)))
        b, l = metricas_ou(lam, tot_te, linhas_mkt)
        print(f"  Poisson ataque/defesa       MAE={mae_p:.3f}  brierOU={b:.4f}  loglossOU={l:.4f}")
    else:
        print("  Poisson ataque/defesa       (lam_poisson sem dados)")

    # ---- modelo ----
    print("\nMODELO (teste):")
    pipe = Pipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("sc", StandardScaler()),
        ("reg", PoissonRegressor(alpha=1.0, max_iter=500)),
    ])
    pipe.fit(Xtr, ytr)
    pred = pipe.predict(Xte)
    mae = float(np.mean(np.abs(yte - pred)))
    b, l = metricas_ou(pred, tot_te, linhas_mkt)
    print(f"  PoissonRegressor            MAE={mae:.3f}  brierOU={b:.4f}  loglossOU={l:.4f}")

    # ---- re-treina em tudo e salva ----
    pipe.fit(X, y)
    out = os.path.join(PASTA, f"modelo_{mercado}.joblib")
    joblib.dump({"pipeline": pipe, "colunas": cols, "mercado": mercado,
                 "n_treino": n, "linhas": linhas_mkt}, out)
    print(f"\n🏆 Modelo salvo em {out}")

    reg = pipe.named_steps["reg"]
    if hasattr(reg, "coef_"):
        ordem = np.argsort(np.abs(reg.coef_))[::-1]
        print("\nTop features (|coef|):")
        for i in ordem[:8]:
            print(f"  {cols[i]:<20} {reg.coef_[i]:+.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mercado", choices=list(MK.MERCADOS), default="escanteios")
    ap.add_argument("--teste", type=float, default=0.2)
    a = ap.parse_args()
    treinar(a.mercado, a.teste)
