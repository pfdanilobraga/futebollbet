# -*- coding: utf-8 -*-
"""
Treino e avaliação do modelo de ML (Estágio 3) — previsão 1X2.

- Validação WALK-FORWARD (TimeSeriesSplit): re-treina em janelas crescentes e
  prevê a janela seguinte, acumulando previsões out-of-fold (OOF). Substitui o
  antigo split único 80/20 — estimativa de generalização muito mais confiável e
  imune a "sorte no corte". Sem vazamento temporal (features.py é cronológico).
- Dois modelos: Regressão Logística (baseline robusto) e XGBoost.
- Imputação de features faltantes (mediana) dentro de um Pipeline. A flag
  `tem_odds` (em features.py) separa o regime sem-mercado, para a imputação não
  injetar probabilidade implícita falsa nos jogos sem odds.
- Métricas: RPS (Ranked Probability Score — métrica ordinal padrão para 1X2),
  log-loss, Brier e acurácia. Reportadas no pool OOF, estratificadas por liga e
  por regime com-odds / sem-odds, comparadas a:
    * baseline ingênuo (taxas-base do treino de cada fold)
    * baseline de mercado (odds implícitas), nos jogos que têm odds.
- Seleção do modelo final por LOG-LOSS (penaliza overconfidence — adequado a
  apostas), re-treinado em todos os dados e salvo em `modelo.joblib`.
- Exporta as previsões OOF em `oof_preds.csv` para o backtest de EV/CLV.

Uso:
  py treino.py                 # treina, avalia (walk-forward) e salva
  py treino.py --splits 5      # nº de janelas do walk-forward (padrão 5)
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
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import log_loss, accuracy_score
from scipy.optimize import minimize_scalar
from xgboost import XGBClassifier

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import features as F

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
MODELO_PATH = os.path.join(PASTA, "modelo.joblib")
OOF_PATH = os.path.join(PASTA, "oof_preds.csv")
CLASSES = ["H", "D", "A"]            # ordem ordinal (H < D < A) usada pelo RPS
MIN_TREINO = 50                      # nº mínimo de jogos p/ treinar de forma séria
MIN_JOGOS_TEMPORADA = 10             # temporadas menores (ex.: copa de 1 jogo) são lixo
N_SPLITS = 5                         # janelas do walk-forward
ODDS_COLS = ["imp_casa", "imp_empate", "imp_fora"]


# ----------------------------------------------------------------- métricas
def brier_multiclasse(y_true_idx, probas):
    """Média, sobre as amostras, de sum_k (p_k - y_k)^2. Quanto menor, melhor.
    Varia de 0 (perfeito) a 2 (péssimo). Baseline aleatório ~0.66."""
    onehot = np.zeros_like(probas)
    onehot[np.arange(len(y_true_idx)), y_true_idx] = 1
    return np.mean(np.sum((probas - onehot) ** 2, axis=1))


def rps_multiclasse(y_true_idx, probas):
    """Ranked Probability Score para resultado ordinal (H < D < A).
    RPS = 1/(r-1) * média_amostras( sum_{i=1}^{r-1} (CDF_pred_i - CDF_real_i)^2 ).
    Penaliza errar por '2 casas' (prever casa quando deu fora) mais que por 1 casa
    (prever casa quando deu empate) — é a métrica padrão da literatura para 1X2.
    0 = perfeito; ~0.22 para chute pelas taxas-base."""
    onehot = np.zeros_like(probas)
    onehot[np.arange(len(y_true_idx)), y_true_idx] = 1
    cum_p = np.cumsum(probas, axis=1)
    cum_y = np.cumsum(onehot, axis=1)
    # as r-1 primeiras posições cumulativas (a última é sempre 1, não contribui)
    return np.mean(np.sum((cum_p[:, :-1] - cum_y[:, :-1]) ** 2, axis=1)) / (probas.shape[1] - 1)


def aplicar_temperatura(probas, T):
    """Temperature scaling a partir de probabilidades: p^(1/T) renormalizado.
    T>1 'amolece' (menos confiante); T<1 'afia'. T=1 não muda nada."""
    p = np.clip(probas, 1e-12, None) ** (1.0 / T)
    return p / p.sum(axis=1, keepdims=True)


def ajustar_temperatura(probas, y_idx):
    """Acha o T (em [0.5, 5]) que minimiza o log-loss out-of-fold."""
    def nll(T):
        return log_loss(y_idx, aplicar_temperatura(probas, T), labels=[0, 1, 2])
    res = minimize_scalar(nll, bounds=(0.5, 5.0), method="bounded")
    return float(res.x)


def avaliar(nome, y_idx, probas):
    """Imprime e devolve (logloss, rps, brier, acuracia)."""
    probas = probas / probas.sum(axis=1, keepdims=True)   # normaliza (defensivo)
    ll = log_loss(y_idx, probas, labels=[0, 1, 2])
    rp = rps_multiclasse(y_idx, probas)
    br = brier_multiclasse(y_idx, probas)
    ac = accuracy_score(y_idx, probas.argmax(axis=1))
    print(f"  {nome:<26} logloss={ll:.4f}  rps={rp:.4f}  brier={br:.4f}  acuracia={ac:.1%}")
    return ll, rp, br, ac


# ----------------------------------------------------------------- dados
def carregar_df():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        linhas = F.construir_dataset(con)
    finally:
        con.close()
    df = pd.DataFrame(linhas)
    df = df[df["alvo"].notna()].reset_index(drop=True)
    # descarta temporadas minúsculas (ex.: Nedbank Cup com 1 jogo) que poluem
    # as features de liga e não dão para validar
    cont = df["temporada_id"].value_counts()
    descartar = cont[cont < MIN_JOGOS_TEMPORADA]
    if len(descartar):
        print(f"Temporadas excluídas (< {MIN_JOGOS_TEMPORADA} jogos): "
              f"{dict(descartar.astype(int))}")
        df = df[df["temporada_id"].isin(cont[cont >= MIN_JOGOS_TEMPORADA].index)]
    df = df.sort_values(["inicio_ts", "evento_id"]).reset_index(drop=True)
    return df


def descartar_features_vazias(df, cols):
    """Remove features 100% ausentes (ex.: xG/odds ainda não coletados),
    para não poluir o modelo. Retorna a lista de colunas mantidas."""
    mantidas = [c for c in cols if df[c].notna().any()]
    descartadas = [c for c in cols if c not in mantidas]
    if descartadas:
        print(f"Features ignoradas (sem dados ainda): {', '.join(descartadas)}")
    return mantidas


# ----------------------------------------------------------------- modelos
def construir_modelos():
    """Pipelines novos a cada chamada (sem estado entre folds)."""
    return {
        "logística": Pipeline([
            ("imp", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, C=0.5)),
        ]),
        "xgboost": Pipeline([
            ("imp", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("clf", XGBClassifier(
                n_estimators=180, max_depth=3, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, min_child_weight=3,
                reg_lambda=1.5, objective="multi:softprob", num_class=3,
                eval_metric="mlogloss", tree_method="hist", verbosity=0)),
        ]),
    }


def _proba_ordenada(pipe, X):
    """predict_proba reordenado para a ordem CLASSES (0=H,1=D,2=A)."""
    proba = pipe.predict_proba(X)
    classes_ = pipe.named_steps["clf"].classes_
    idx = [list(classes_).index(i) for i in range(3)]
    return proba[:, idx]


def walk_forward(X, y, n_splits=N_SPLITS):
    """Re-treina em janelas crescentes e prevê a janela seguinte.
    Retorna (oof, previsto, ultimo):
      oof[nome]  -> array (n,3) de probabilidades OOF (NaN onde não previsto)
      previsto   -> máscara booleana das linhas com previsão OOF
      ultimo     -> máscara do ÚLTIMO fold (janela madura: maior treino, ~prod)
    O baseline 'ingênuo' usa as taxas-base do treino de cada fold."""
    n = len(X)
    tscv = TimeSeriesSplit(n_splits=n_splits)
    nomes = list(construir_modelos().keys())
    oof = {nome: np.full((n, 3), np.nan) for nome in nomes}
    oof["ingênuo"] = np.full((n, 3), np.nan)
    previsto = np.zeros(n, dtype=bool)
    ultimo = np.zeros(n, dtype=bool)

    folds = list(tscv.split(X))
    for k, (tr_idx, te_idx) in enumerate(folds):
        previsto[te_idx] = True
        if k == len(folds) - 1:
            ultimo[te_idx] = True
        taxas = np.bincount(y[tr_idx], minlength=3) / len(tr_idx)
        oof["ingênuo"][te_idx] = taxas
        for nome, pipe in construir_modelos().items():
            pipe.fit(X.iloc[tr_idx], y[tr_idx])
            oof[nome][te_idx] = _proba_ordenada(pipe, X.iloc[te_idx])
    return oof, previsto, ultimo


def _baseline_mercado(X, prev_mask):
    """Máscara e probabilidades do mercado (odds implícitas) nas linhas previstas
    que TÊM odds. Retorna (mask_alinhada_a_prev, probas) ou (None, None)."""
    if not all(c in X.columns for c in ODDS_COLS):
        return None, None
    tem = X[ODDS_COLS].notna().all(axis=1).values & prev_mask
    if tem.sum() < 10:
        return None, None
    return tem, X.loc[tem, ODDS_COLS].values


# ----------------------------------------------------------------- treino
def treinar(n_splits=N_SPLITS):
    df = carregar_df()
    n = len(df)
    print(f"{n} jogos finalizados.  Alvo: "
          f"{(df['alvo']=='H').mean():.0%} casa / "
          f"{(df['alvo']=='D').mean():.0%} empate / "
          f"{(df['alvo']=='A').mean():.0%} fora")
    if n < MIN_TREINO:
        print(f"\n⚠️  Apenas {n} jogos — insuficiente para um modelo confiável "
              f"(ideal: 1000+ com detalhes). Treinando assim mesmo para validar "
              f"o pipeline; os números servem só de referência.")

    cols = descartar_features_vazias(df, F.COLUNAS)
    y = df["alvo"].map({c: i for i, c in enumerate(CLASSES)}).values
    X = df[cols].astype(float)

    # --------- walk-forward (out-of-fold) ---------
    oof, prev, ult = walk_forward(X, y, n_splits)
    yv = y[prev]
    print(f"\nWALK-FORWARD ({n_splits} janelas):  {prev.sum()} jogos avaliados "
          f"out-of-fold (de {n}).\n")

    print("AVALIAÇÃO (out-of-fold — todos os jogos avaliados):")
    metr = {}
    for nome in ("ingênuo", "logística", "xgboost"):
        metr[nome] = avaliar(nome, yv, oof[nome][prev])
    mmask, mproba = _baseline_mercado(X, prev)
    if mmask is not None:
        avaliar(f"mercado/odds (n={mmask.sum()})", y[mmask], mproba)
    else:
        print("  mercado/odds              (odds insuficientes — colete com --detalhes)")

    # --------- janela madura (último fold: maior treino, mais perto de produção) ---------
    if ult.sum() >= 10:
        print(f"\n  -- janela madura: último fold (treino grande, n={ult.sum()}) --")
        for nome in ("ingênuo", "logística", "xgboost"):
            avaliar(nome, y[ult], oof[nome][ult])
        mu = mmask & ult if mmask is not None else None
        if mu is not None and mu.sum() >= 10:
            avaliar(f"mercado/odds (n={mu.sum()})", y[mu], X.loc[mu, ODDS_COLS].values)

    # --------- estratificado por regime de odds ---------
    if "tem_odds" in cols:
        com = prev & (df["tem_odds"].values == 1.0)
        sem = prev & (df["tem_odds"].values == 0.0)
        for rotulo, mask in (("COM odds", com), ("SEM odds", sem)):
            if mask.sum() >= 10:
                print(f"\n  -- só jogos {rotulo} (n={mask.sum()}) --")
                for nome in ("ingênuo", "logística", "xgboost"):
                    avaliar(nome, y[mask], oof[nome][mask])

    # --------- estratificado por liga ---------
    print("\n  -- RPS por liga (out-of-fold) --")
    for tid, sub in df[prev].groupby("temporada_id"):
        idx = sub.index.values
        linha = f"    temp {int(tid):>6} (n={len(idx):>3}):"
        for nome in ("ingênuo", "logística", "xgboost"):
            linha += f"  {nome[:3]}={rps_multiclasse(y[idx], oof[nome][idx]):.4f}"
        print(linha)

    # --------- escolhe melhor por LOG-LOSS e re-treina em tudo ---------
    melhor = min(("logística", "xgboost"), key=lambda k: metr[k][0])
    print(f"\n🏆 Melhor por log-loss (OOF): {melhor}. "
          f"Re-treinando em todos os {n} jogos…")

    # --------- temperature scaling: corrige overconfiança (estrela-guia) ---------
    T = ajustar_temperatura(oof[melhor][prev], yv)
    print(f"\nTemperature scaling (fit em OOF):  T={T:.3f}  (T>1 = menos confiante)")
    avaliar(f"{melhor} (T=1)   ", yv, oof[melhor][prev])
    avaliar(f"{melhor} (T={T:.2f})", yv, aplicar_temperatura(oof[melhor][prev], T))

    final = construir_modelos()[melhor]
    final.fit(X, y)
    joblib.dump({"pipeline": final, "colunas": cols, "classes": CLASSES,
                 "n_treino": n, "modelo": melhor, "temperatura": T}, MODELO_PATH)
    print(f"\nModelo salvo em {MODELO_PATH}")

    _salvar_oof(df, oof, prev, melhor, T)
    _importancias(final, cols)


def _salvar_oof(df, oof, prev, melhor, T=1.0):
    """Exporta as previsões OOF (já calibradas por temperatura, como em produção)
    do melhor modelo p/ o backtest de EV/CLV."""
    sub = df[prev].copy()
    p = aplicar_temperatura(oof[melhor][prev], T)
    out = pd.DataFrame({
        "evento_id": sub["evento_id"].values,
        "temporada_id": sub["temporada_id"].values,
        "inicio_ts": sub["inicio_ts"].values,
        "alvo": sub["alvo"].values,
        "modelo": melhor,
        "p_casa": p[:, 0], "p_empate": p[:, 1], "p_fora": p[:, 2],
    })
    out.to_csv(OOF_PATH, index=False)
    print(f"Previsões OOF salvas em {OOF_PATH} ({len(out)} jogos)")


def _importancias(pipe, cols):
    clf = pipe.named_steps["clf"]
    imp = None
    if hasattr(clf, "feature_importances_"):
        imp = clf.feature_importances_
    elif hasattr(clf, "coef_"):
        imp = np.abs(clf.coef_).mean(axis=0)
    if imp is None:
        return
    ordem = np.argsort(imp)[::-1]
    print("\nTop features:")
    for i in ordem[:10]:
        print(f"  {cols[i]:<16} {imp[i]:.3f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", type=int, default=N_SPLITS)
    a = ap.parse_args()
    treinar(a.splits)
