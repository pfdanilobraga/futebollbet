# -*- coding: utf-8 -*-
"""
Previsão 1X2 com o modelo de ML treinado (Estágio 3).

Carrega `modelo.joblib`, calcula as features do evento (mesma lógica do treino,
sem vazamento) e grava a previsão na tabela `probabilidade` com modelo='ml'.

Uso:
  py prever.py 15526121         # um evento
  py prever.py --proximos       # todos os jogos não iniciados no banco

Pré-requisito: treinar antes com `py treino.py`.
"""
import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import joblib

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import features as F

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
MODELO_PATH = os.path.join(PASTA, "modelo.joblib")


def carregar_modelo():
    if not os.path.exists(MODELO_PATH):
        sys.exit("Modelo não encontrado. Rode antes:  py treino.py")
    return joblib.load(MODELO_PATH)


def prever_evento(con, modelo, evento_id, verboso=True):
    nomes = con.execute(
        """SELECT tc.nome, tf.nome FROM evento e
           JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
           WHERE e.id=?""", (evento_id,)).fetchone()
    if not nomes:
        print(f"Evento {evento_id} não está no banco.")
        return
    feat = F.features_para_evento(con, evento_id)
    if feat is None:
        print(f"Evento {evento_id}: sem features.")
        return
    X = pd.DataFrame([{c: feat.get(c) for c in modelo["colunas"]}]).astype(float)
    pipe = modelo["pipeline"]
    proba = pipe.predict_proba(X)[0]
    classes_ = list(pipe.named_steps["clf"].classes_)
    p = {modelo["classes"][classes_.index(i)]: proba[classes_.index(i)]
         for i in range(len(modelo["classes"]))}
    # garante ordem H/D/A
    pc, pe, pf = p.get("H", 0), p.get("D", 0), p.get("A", 0)
    # temperature scaling (mesma calibração avaliada no walk-forward)
    T = modelo.get("temperatura", 1.0)
    if T and T != 1.0:
        v = np.clip(np.array([pc, pe, pf], dtype=float), 1e-12, None) ** (1.0 / T)
        pc, pe, pf = (v / v.sum())

    con.execute(
        """INSERT INTO probabilidade
               (evento_id, modelo, p_casa, p_empate, p_fora, detalhes_json, calculado_em)
           VALUES (?, 'ml', ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(evento_id, modelo) DO UPDATE SET
               p_casa=excluded.p_casa, p_empate=excluded.p_empate,
               p_fora=excluded.p_fora, detalhes_json=excluded.detalhes_json,
               calculado_em=datetime('now')""",
        (evento_id, round(float(pc), 4), round(float(pe), 4), round(float(pf), 4),
         json.dumps({"algoritmo": modelo["modelo"],
                     "n_treino": modelo["n_treino"]})))
    con.commit()
    if verboso:
        print(f"\n⚽ {nomes[0]} x {nomes[1]}  (evento {evento_id})")
        print(f"   modelo ML ({modelo['modelo']}):  "
              f"casa {pc:.1%}  |  empate {pe:.1%}  |  fora {pf:.1%}")
    return (pc, pe, pf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("evento_id", type=int, nargs="?")
    ap.add_argument("--proximos", action="store_true")
    a = ap.parse_args()

    modelo = carregar_modelo()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        if a.proximos:
            ids = [r[0] for r in con.execute(
                "SELECT id FROM evento WHERE status='notstarted' ORDER BY inicio_ts")]
            print(f"{len(ids)} jogos não iniciados.")
            for eid in ids:
                prever_evento(con, modelo, eid)
        elif a.evento_id:
            prever_evento(con, modelo, a.evento_id)
        else:
            ap.error("informe um evento_id ou use --proximos")
    finally:
        con.close()


if __name__ == "__main__":
    main()
