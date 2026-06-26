# -*- coding: utf-8 -*-
"""
Coletor de xG por chute do Understat (Big-5 europeu), via understatapi (SEM
Cloudflare — funciona com requests, diferente do SofaScore). Adiciona uma fonte
de xG INDEPENDENTE e densa para as ligas top, populando a tabela `chute`.

PROBLEMA DE IDENTIDADE: Understat nomeia times ("Manchester United") diferente
do football-data ("Man United") e do SofaScore. Para anexar os chutes ao evento
certo SEM duplicar nem arriscar merge errado, casa-se a partida por TRÍPLICE
restrição de alta confiança:  data (±2 dias) + PLACAR FINAL EXATO + nome fuzzy
dos dois times. Só anexa quando há exatamente 1 evento compatível.

Understat é fonte de ENRIQUECIMENTO (xG), não de resultado: se não achar o
evento (ex.: liga não ingerida), apenas pula — nunca cria evento novo.

Uso:
  py coletor_understat.py --teste                 # EPL temporada corrente (smoke)
  py coletor_understat.py --ligas EPL,La_liga --temporadas 2023,2024
  py coletor_understat.py --tudo --temporadas 2022,2023,2024
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys
import time as _time

from rapidfuzz import fuzz
from understatapi import UnderstatClient

import mapeamento_times as mt

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")

# liga Understat -> div football-data (p/ escopar os eventos candidatos ao casar)
LIGA_DIV = {
    "EPL": "E0", "La_Liga": "SP1", "Bundesliga": "D1",
    "Serie_A": "I1", "Ligue_1": "F1",
}
# Understat result -> chute.tipo (convenção do schema: goal|save|miss|block|post)
RESULT_TIPO = {
    "Goal": "goal", "OwnGoal": "goal", "MissedShots": "miss",
    "SavedShot": "save", "BlockedShot": "block", "ShotOnPost": "post",
}
NAME_FUZZY_PISO = 60    # piso do nome (placar+data já são quase únicos por rodada)
NAME_FUZZY_MARGEM = 8   # melhor candidato tem que vencer o 2º por esta margem (anti-ambiguidade)


def conectar():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _div_torneio_id(div):
    import hashlib
    h = hashlib.sha1(f"fdcouk|{div}".encode()).hexdigest()
    return -(int(h[:15], 16) & ((1 << 62) - 1)) - 1


def _indexar_eventos(con, torneio_id):
    """(gols_casa, gols_fora) -> [(ts, casa_norm, fora_norm, evento_id), ...] do torneio."""
    idx = {}
    for eid, ts, gc, ga, casa, fora in con.execute(
            """SELECT e.id, e.inicio_ts, e.gols_casa, e.gols_fora, tc.nome, tf.nome
               FROM evento e JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
               WHERE e.torneio_id=? AND e.status='finished'
                 AND e.gols_casa IS NOT NULL AND e.gols_fora IS NOT NULL""", (torneio_id,)):
        idx.setdefault((gc, ga), []).append((ts, mt._norm(casa), mt._norm(fora), eid))
    return idx


def _casar_evento(idx, ts_u, gh, ga, home_norm, away_norm):
    """Casa por placar exato + data próxima; entre candidatos, escolhe o melhor nome.
    Aceita só se o melhor passa o piso E vence o 2º por uma margem (anti-ambiguidade)."""
    scored = []
    for ts, casa_n, fora_n, eid in idx.get((gh, ga), []):
        if abs(ts - ts_u) > 2 * 86400:
            continue
        sc = min(fuzz.token_sort_ratio(home_norm, casa_n),
                 fuzz.token_sort_ratio(away_norm, fora_n))
        scored.append((sc, eid))
    if not scored:
        return None
    scored.sort(reverse=True)
    if scored[0][0] < NAME_FUZZY_PISO:
        return None
    if len(scored) > 1 and scored[1][0] >= scored[0][0] - NAME_FUZZY_MARGEM:
        return None                      # dois candidatos quase iguais: não arrisca
    return scored[0][1]


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def coletar_liga(con, cli, liga, temporada, idx):
    div = LIGA_DIV[liga]
    try:
        partidas = cli.league(league=liga).get_match_data(season=str(temporada))
    except Exception as e:
        print(f"[{liga} {temporada}] erro ao listar: {e}")
        return 0, 0, 0
    casados = chutes = sem_evento = 0
    for m in partidas:
        if str(m.get("isResult")).lower() != "true":
            continue
        ts_u = None
        try:
            ts_u = int(dt.datetime.fromisoformat(m["datetime"]).replace(
                tzinfo=dt.timezone.utc).timestamp())
        except Exception:
            continue
        gh, ga = _i(m.get("goals", {}).get("h")), _i(m.get("goals", {}).get("a"))
        home = mt._norm(m.get("h", {}).get("title"))
        away = mt._norm(m.get("a", {}).get("title"))
        if gh is None or ga is None:
            continue
        eid = _casar_evento(idx, ts_u, gh, ga, home, away)
        if eid is None:
            sem_evento += 1
            continue
        try:
            shots = cli.match(match=str(m["id"])).get_shot_data()
            _time.sleep(0.2)             # educado (understatapi não throttle sozinho)
        except Exception:
            continue
        for lado, eh_casa in (("h", 1), ("a", 0)):
            for s in shots.get(lado, []):
                sid = _i(s.get("id"))
                if sid is None:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO chute VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (-sid, eid, None, eh_casa, _i(s.get("minute")),
                     RESULT_TIPO.get(s.get("result")), s.get("situation"),
                     s.get("shotType"),
                     float(s["xG"]) if s.get("xG") else None, None,
                     float(s["X"]) * 100 if s.get("X") else None,
                     float(s["Y"]) * 100 if s.get("Y") else None))
                chutes += 1
        casados += 1
    con.commit()
    print(f"[{liga} {temporada}] {casados} jogos casados, {chutes} chutes (xG); {sem_evento} sem evento no banco")
    return casados, chutes, sem_evento


def main():
    ap = argparse.ArgumentParser(description="Coletor Understat (xG por chute, Big-5).")
    ap.add_argument("--ligas", help="ligas Understat (EPL,La_liga,Bundesliga,Serie_A,Ligue_1)")
    ap.add_argument("--temporadas", help="anos Understat separados por vírgula (ex.: 2023,2024)")
    ap.add_argument("--tudo", action="store_true", help="todas as 5 ligas Big-5")
    ap.add_argument("--teste", action="store_true", help="smoke: EPL temporada corrente")
    a = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit("futebol.db não existe.")
    ano_corr = dt.date.today().year if dt.date.today().month >= 7 else dt.date.today().year - 1

    if a.teste:
        ligas, temporadas = ["EPL"], [ano_corr]
    else:
        ligas = list(LIGA_DIV) if a.tudo else (
            [x.strip() for x in a.ligas.split(",")] if a.ligas else list(LIGA_DIV))
        temporadas = ([int(x) for x in a.temporadas.split(",")] if a.temporadas else [ano_corr])

    con = conectar()
    cli = UnderstatClient()
    tot_c = tot_s = 0
    try:
        with cli:
            for liga in ligas:
                if liga not in LIGA_DIV:
                    print(f"(liga desconhecida: {liga})")
                    continue
                idx = _indexar_eventos(con, _div_torneio_id(div=LIGA_DIV[liga]))
                for temp in temporadas:
                    c, ch, s = coletar_liga(con, cli, liga, temp, idx)
                    tot_c += c
                    tot_s += s
    finally:
        con.close()
    print(f"\nTotal: {tot_c} jogos enriquecidos com xG Understat; {tot_s} não encontrados no banco.")


if __name__ == "__main__":
    main()
