# -*- coding: utf-8 -*-
"""
Engenharia de features para o ML de mercados de CONTAGEM (escanteios, chutes,
chutes ao gol, cartões) — alvo = TOTAL da partida (casa+fora) da estatística.

Mesmo princípio do features.py (1X2): ZERO VAZAMENTO. As features de uma partida
usam só dados de partidas ANTERIORES ao apito. A mesma função serve treino e
previsão (sem train/serve skew).

A fonte da estatística é `evento_estatistica` (periodo='ALL'); os nomes vêm da
config MERCADOS de mercados.py (sinônimos por idioma/liga).

Features (todas NULL-safe -> None quando faltam dados):
  - media_para_casa, media_contra_casa . média recente da stat do mandante em casa
  - media_para_fora, media_contra_fora . idem do visitante fora
  - lam_poisson .......................... λ_total do modelo Poisson (mercados.py) — o "prior"
  - media_total_casa, media_total_fora ... média do TOTAL nos jogos recentes de cada time
  - liga_media_total ..................... média do total na temporada (contexto)
  - descanso_casa, descanso_fora ......... dias desde o último jogo
  - h2h_total ............................ média do total nos confrontos diretos anteriores

Alvo: `total` (real) + metadados. O treino converte média prevista -> P(Over/Under).

Uso (diagnóstico):
  py features_mercado.py                 # escanteios (default)
  py features_mercado.py --mercado chutes
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
JANELA = 8          # nº de jogos recentes nas médias por mando

import mercados as MK   # config de mercados + prob_poisson (prior)

COLUNAS = [
    "media_para_casa", "media_contra_casa", "media_para_fora", "media_contra_fora",
    "lam_poisson", "media_total_casa", "media_total_fora",
    "liga_media_total", "descanso_casa", "descanso_fora", "h2h_total",
]


class HistoricoMercado:
    """Histórico incremental por time da estatística de contagem (for/against
    por mando), em ordem cronológica — permite features sem vazamento."""

    MIN_JOGOS = 3        # mínimo no mando p/ confiar no prior Poisson (igual mercados.py)

    def __init__(self, con, nomes):
        self.con = con
        self.nomes = nomes
        self._totais = self._carregar_totais()      # evento_id -> (casa_valor, fora_valor)
        self.por_time = defaultdict(list)            # time -> [{ts, mando, para, contra, total, adversario}]
        # liga acumulada SÓ com jogos já vistos (sem vazamento), por temporada:
        # temporada_id -> [soma_casa, soma_fora, n]
        self.liga = defaultdict(lambda: [0.0, 0.0, 0])

    def _carregar_totais(self):
        ph = ",".join("?" * len(self.nomes))
        d = {}
        for eid, cv, fv in self.con.execute(
                f"""SELECT evento_id, casa_valor, fora_valor FROM evento_estatistica
                    WHERE periodo='ALL' AND nome IN ({ph}) AND casa_valor IS NOT NULL""",
                self.nomes):
            d[eid] = (cv, fv)
        return d

    @staticmethod
    def _media_n(jogos, campo, mando=None, n=None):
        """(média, quantidade) — quantidade permite exigir amostra mínima."""
        sel = [j for j in jogos if mando is None or j["mando"] == mando]
        if n:
            sel = sel[-n:]
        return (sum(j[campo] for j in sel) / len(sel), len(sel)) if sel else (None, 0)

    def _media(self, jogos, campo, mando=None, n=None):
        return self._media_n(jogos, campo, mando, n)[0]

    def _h2h_total(self, casa, fora, ts):
        vals = [j["total"] for j in self.por_time[casa]
                if j["adversario"] == fora and j["ts"] < ts]
        return sum(vals) / len(vals) if vals else None

    def _prior_poisson(self, ev, hc, hf):
        """λ_total leakage-safe: força ataque/defesa do histórico já visto +
        média de liga acumulada. Mesma convenção do mercados.prob_poisson.
        None se liga vazia ou amostra de qualquer lado < MIN_JOGOS."""
        soma_c, soma_f, n_liga = self.liga[ev.get("temporada_id")]
        if n_liga < self.MIN_JOGOS:
            return None, None
        liga_casa, liga_fora = soma_c / n_liga, soma_f / n_liga
        if not liga_casa or not liga_fora:
            return None, None
        atq_c, nc = self._media_n(hc, "para", "casa", JANELA)
        def_c, _ = self._media_n(hc, "contra", "casa", JANELA)
        atq_f, nf = self._media_n(hf, "para", "fora", JANELA)
        def_f, _ = self._media_n(hf, "contra", "fora", JANELA)
        if None in (atq_c, def_c, atq_f, def_f) or nc < self.MIN_JOGOS or nf < self.MIN_JOGOS:
            return None, (liga_casa + liga_fora)
        lam = atq_c * def_f / liga_casa + atq_f * def_c / liga_fora
        return lam, (liga_casa + liga_fora)

    def features(self, ev):
        casa, fora, ts = ev["casa_id"], ev["fora_id"], ev["inicio_ts"]
        hc, hf = self.por_time[casa], self.por_time[fora]
        ult_c = hc[-1]["ts"] if hc else None
        ult_f = hf[-1]["ts"] if hf else None
        lam, liga_total = self._prior_poisson(ev, hc, hf)   # leakage-safe (só passado)
        return {
            "media_para_casa":    self._media(hc, "para", "casa", JANELA),
            "media_contra_casa":  self._media(hc, "contra", "casa", JANELA),
            "media_para_fora":    self._media(hf, "para", "fora", JANELA),
            "media_contra_fora":  self._media(hf, "contra", "fora", JANELA),
            "lam_poisson":        lam,
            "media_total_casa":   self._media(hc, "total", None, JANELA),
            "media_total_fora":   self._media(hf, "total", None, JANELA),
            "liga_media_total":   liga_total,
            "descanso_casa":      (ts - ult_c) / 86400 if ult_c else None,
            "descanso_fora":      (ts - ult_f) / 86400 if ult_f else None,
            "h2h_total":          self._h2h_total(casa, fora, ts),
        }

    def registrar(self, ev):
        t = self._totais.get(ev["id"])
        if not t:
            return                       # sem a estatística -> não entra no histórico
        cv, fv = t
        total = cv + fv
        self.por_time[ev["casa_id"]].append(dict(
            ts=ev["inicio_ts"], mando="casa", para=cv, contra=fv,
            total=total, adversario=ev["fora_id"]))
        self.por_time[ev["fora_id"]].append(dict(
            ts=ev["inicio_ts"], mando="fora", para=fv, contra=cv,
            total=total, adversario=ev["casa_id"]))
        lg = self.liga[ev.get("temporada_id")]      # acumula liga (só jogos já vistos)
        lg[0] += cv; lg[1] += fv; lg[2] += 1


def construir_dataset(con, mercado="escanteios"):
    """Percorre jogos finalizados em ordem cronológica e devolve linhas:
    features + 'total' (alvo) + metadados. Só inclui jogos com a estatística."""
    nomes = MK.MERCADOS[mercado]["stat"]
    eventos = [dict(r) for r in con.execute(
        """SELECT id, casa_id, fora_id, inicio_ts, temporada_id
           FROM evento WHERE status='finished' ORDER BY inicio_ts, id""")]
    hist = HistoricoMercado(con, nomes)
    linhas = []
    for ev in eventos:
        t = hist._totais.get(ev["id"])
        if t is not None:                       # só jogos com a estatística viram amostra
            feat = hist.features(ev)            # ANTES de registrar (sem vazamento)
            feat["total"] = t[0] + t[1]
            feat["evento_id"] = ev["id"]
            feat["inicio_ts"] = ev["inicio_ts"]
            feat["temporada_id"] = ev["temporada_id"]
            linhas.append(feat)
        hist.registrar(ev)
    return linhas


def features_para_evento(con, evento_id, mercado="escanteios"):
    """Features de UM evento (futuro), usando todo o histórico finalizado anterior."""
    nomes = MK.MERCADOS[mercado]["stat"]
    ev = con.execute(
        "SELECT id, casa_id, fora_id, inicio_ts, temporada_id FROM evento WHERE id=?",
        (evento_id,)).fetchone()
    if not ev:
        return None
    ev = dict(zip(["id", "casa_id", "fora_id", "inicio_ts", "temporada_id"], ev))
    hist = HistoricoMercado(con, nomes)
    for r in con.execute(
            """SELECT id, casa_id, fora_id, inicio_ts, temporada_id FROM evento
               WHERE status='finished' AND inicio_ts < ? ORDER BY inicio_ts, id""",
            (ev["inicio_ts"],)):
        hist.registrar(dict(zip(
            ["id", "casa_id", "fora_id", "inicio_ts", "temporada_id"], r)))
    return hist.features(ev)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mercado", choices=list(MK.MERCADOS), default="escanteios")
    a = ap.parse_args()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    ds = construir_dataset(con, a.mercado)
    print(f"{len(ds)} jogos com a estatística '{a.mercado}'.")
    if ds:
        n = len(ds)
        media = sum(r["total"] for r in ds) / n
        print(f"Total médio: {media:.2f}")
        print("\nCobertura por feature (% de jogos com valor):")
        for col in COLUNAS:
            preench = sum(1 for r in ds if r.get(col) is not None)
            print(f"  {col:<20} {preench/n:6.1%}")
