# -*- coding: utf-8 -*-
"""
Avalia SOB DEMANDA uma liga específica: puxa as odds correntes dela no the-odds-api
(1 crédito), roda o modelo e mostra os jogos futuros com a previsão + odds de mercado.

Pensado p/ ECONOMIZAR crédito: em vez de puxar todas as ligas todo dia (~450/mês),
você só "sinaliza" a liga que quer avaliar agora. 1 crédito por liga (traz todos os
jogos futuros dela).

Uso:
  py avaliar.py                 # lista as ligas ATIVAS (não gasta crédito)
  py avaliar.py premier         # avalia a Premier League (1 crédito)
  py avaliar.py brasileirao     # Brasileirão Série A
  py avaliar.py libertadores
  py avaliar.py "serie a"       # Serie A (Itália)
"""
import datetime as dt
import sys

import requests
from rapidfuzz import fuzz, process

import coletor_oddsapi as OA
import features as F
import mapeamento_times
import prever as PV

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# atalhos amigáveis -> sport_key (só valem se a liga estiver em temporada)
ALIASES = {
    "premier": "soccer_epl", "epl": "soccer_epl", "inglaterra": "soccer_epl",
    "brasileirao": "soccer_brazil_campeonato", "brasil": "soccer_brazil_campeonato",
    "serie a brasil": "soccer_brazil_campeonato", "serieb": "soccer_brazil_serie_b",
    "serie b": "soccer_brazil_serie_b",
    "libertadores": "soccer_conmebol_copa_libertadores",
    "sudamericana": "soccer_conmebol_copa_sudamericana",
    "italia": "soccer_italy_serie_a", "serie a": "soccer_italy_serie_a",
    "championship": "soccer_efl_champ",
    "espanha": "soccer_spain_la_liga", "la liga": "soccer_spain_la_liga",
    "alemanha": "soccer_germany_bundesliga", "bundesliga": "soccer_germany_bundesliga",
    "franca": "soccer_france_ligue_one", "ligue 1": "soccer_france_ligue_one",
    "mundial": "soccer_fifa_world_cup", "copa do mundo": "soccer_fifa_world_cup",
    "china": "soccer_china_superleague",
}


def ligas_ativas(key):
    r = requests.get(f"{OA.BASE}/sports", params={"apiKey": key}, timeout=40)
    r.raise_for_status()
    return [(s["key"], s["title"]) for s in r.json() if s.get("group") == "Soccer"]


def resolver(termo, ativas):
    termo = termo.strip().lower()
    chaves = {k for k, _ in ativas}
    if termo in ALIASES and ALIASES[termo] in chaves:
        return next((k, t) for k, t in ativas if k == ALIASES[termo])
    for k, t in ativas:                       # key exata (com/sem prefixo soccer_)
        if termo == k or f"soccer_{termo}" == k:
            return k, t
    best = process.extractOne(termo, [t for _, t in ativas], scorer=fuzz.WRatio)
    if best and best[1] >= 65:
        return ativas[best[2]]
    return None


def merc(con, eid):
    o = {e: v for e, v in con.execute(
        "SELECT escolha, odd_decimal FROM odd WHERE evento_id=? AND mercado='Full time' AND parametro=''",
        (eid,))}
    return (o.get("1"), o.get("X"), o.get("2")) if all(k in o for k in ("1", "X", "2")) else None


def main():
    key = OA._api_key()
    if not key:
        sys.exit("THE_ODDS_API_KEY ausente no .env. Cadastre grátis em https://the-odds-api.com")
    ativas = ligas_ativas(key)                # /sports NÃO gasta crédito
    if len(sys.argv) < 2:
        print("Ligas ATIVAS agora (passe um nome p/ avaliar — gasta 1 crédito/liga):\n")
        for k, t in sorted(ativas, key=lambda x: x[1]):
            print(f"  {t:<34} {k}")
        return

    achou = resolver(" ".join(sys.argv[1:]), ativas)
    if not achou:
        print(f"Não achei liga ATIVA para '{' '.join(sys.argv[1:])}'.")
        print("Rode  py avaliar.py  (sem argumento) p/ ver a lista.")
        return
    sk, titulo = achou
    print(f"Avaliando: {titulo}  ({sk}) — 1 crédito the-odds-api...\n")
    con = OA.conectar()
    mtch = mapeamento_times.Matcher(con)
    OA.coletar_sport(con, mtch, key, sk)      # puxa odds (1 crédito) -> cria/atualiza eventos
    con.commit()

    tid = OA._neg(OA.FONTE, sk)
    agora = int(dt.datetime.now().timestamp()) - 3 * 3600
    ids = [r[0] for r in con.execute(
        "SELECT id FROM evento WHERE torneio_id=? AND status='notstarted' AND inicio_ts>=? "
        "ORDER BY inicio_ts", (tid, agora))]
    if not ids:
        print("\nNenhum jogo futuro nessa liga no momento.")
        con.close()
        return

    modelo = PV.carregar_modelo()
    feats = F.features_para_eventos(con, ids)
    print(f"\n{'quando':<14}{'jogo':<36}{'modelo C/E/F':<22}{'mercado 1/X/2'}")
    print("-" * 90)
    for eid in ids:
        PV.prever_evento(con, modelo, eid, verboso=False, feat=feats.get(eid))
        r = con.execute(
            """SELECT e.inicio_ts, tc.nome, tf.nome, p.p_casa, p.p_empate, p.p_fora
               FROM evento e JOIN time tc ON tc.id=e.casa_id JOIN time tf ON tf.id=e.fora_id
               JOIN probabilidade p ON p.evento_id=e.id AND p.modelo='ml' WHERE e.id=?""",
            (eid,)).fetchone()
        if not r:
            continue
        ts, casa, fora, pc, pe, pf = r
        quando = dt.datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")
        palp = max((("casa", pc), ("empate", pe), ("fora", pf)), key=lambda x: x[1])[0]
        mk = merc(con, eid)
        mtxt = f"{mk[0]:.2f}/{mk[1]:.2f}/{mk[2]:.2f}" if mk else "(sem odds)"
        print(f"{quando:<14}{(casa[:16] + ' x ' + fora[:16]):<36}"
              f"{f'{pc:.0%}/{pe:.0%}/{pf:.0%} ->{palp}':<22}{mtxt}")
    con.commit()
    con.close()
    print("\n(modelo = prob. casa/empate/fora; mercado = odds 1X2 atuais. "
          "O modelo acompanha o mercado — use como referência calibrada, não como 'edge'.)")


if __name__ == "__main__":
    main()
