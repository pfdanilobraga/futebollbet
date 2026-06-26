# -*- coding: utf-8 -*-
"""
Coletor de ODDS correntes/pré-jogo do the-odds-api.com (REST, JSON, SEM
Cloudflare). Complementa o football-data.co.uk nas ligas que o CSV NÃO cobre
(Série B BR, J-League, MLS, Libertadores...) e em jogos FUTUROS (o CSV é
histórico). Mapeia 1X2 (h2h) -> tabela `odd` ('Full time' 1/X/2).

⚠️ Requer uma API KEY GRÁTIS (500 créditos/mês): cadastre em
https://the-odds-api.com e exporte THE_ODDS_API_KEY ou crie um arquivo `.env`
no projeto com:  THE_ODDS_API_KEY=sua_chave

Orçamento: 1 crédito por (região × mercado) por chamada. Usamos 1 região (eu)
+ h2h => 1 crédito por liga por execução. 500/mês ~= 16/dia => poucas ligas/dia.
O endpoint /v4/sports (listar) NÃO gasta crédito.

Estratégia: para cada liga, busca odds correntes; resolve times via Matcher
(fonte='theoddsapi'); CASA num evento existente (±36h, mesmos times) p/ anexar
odds sem duplicar; senão cria evento futuro (status notstarted, sem resultado —
o resultado entra depois pelo SofaScore se a liga for raspada).

Uso:
  py coletor_oddsapi.py --listar                 # lista sport_keys de futebol (grátis)
  py coletor_oddsapi.py                           # ligas default (DEFAULT_SPORTS)
  py coletor_oddsapi.py --sports soccer_brazil_serie_b,soccer_japan_j_league
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys

import requests

import gravar
import mapeamento_times

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
BASE = "https://api.the-odds-api.com/v4"
FONTE = "theoddsapi"

# ligas que o football-data.co.uk NÃO cobre bem (foco do complemento)
DEFAULT_SPORTS = [
    "soccer_brazil_campeonato",       # Brasileirão Série A
    "soccer_brazil_serie_b",          # Série B BR
    "soccer_conmebol_copa_libertadores",
    "soccer_japan_j_league",
    "soccer_usa_mls",
]
# casa de aposta preferida (consistência com o projeto): bet365 > pinnacle > média
PREF_BOOKS = ["bet365", "pinnacle", "marathonbet", "williamhill", "betfair_ex_eu"]
# ordem de prioridade quando há mais ligas ativas que o teto diário (grandes primeiro)
PRIORIDADE = [
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_efl_champ",
    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_brazil_campeonato", "soccer_brazil_serie_b",
    "soccer_conmebol_copa_libertadores", "soccer_conmebol_copa_sudamericana",
    "soccer_netherlands_eredivisie", "soccer_portugal_primeira_liga",
    "soccer_usa_mls", "soccer_spain_segunda_division",
]


def _priorizar(ligas):
    """Grandes/continentais primeiro; o resto em ordem alfabética."""
    s = set(ligas)
    return [x for x in PRIORIDADE if x in s] + sorted(x for x in ligas if x not in PRIORIDADE)


def conectar():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _api_key():
    key = os.environ.get("THE_ODDS_API_KEY")
    if key:
        return key.strip()
    env = os.path.join(PASTA, ".env")
    if os.path.exists(env):
        for linha in open(env, encoding="utf-8"):
            if linha.strip().startswith("THE_ODDS_API_KEY"):
                return linha.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _ts(iso):
    """'2026-06-25T19:00:00Z' -> unix ts."""
    try:
        return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _1x2_do_evento(ev):
    """Extrai (odd_casa, odd_empate, odd_fora) do bookmaker preferido disponível."""
    home, away = ev.get("home_team"), ev.get("away_team")
    books = {b.get("key"): b for b in ev.get("bookmakers", [])}
    ordem = PREF_BOOKS + [k for k in books if k not in PREF_BOOKS]
    for bk in ordem:
        b = books.get(bk)
        if not b:
            continue
        for m in b.get("markets", []):
            if m.get("key") != "h2h":
                continue
            preco = {o.get("name"): o.get("price") for o in m.get("outcomes", [])}
            h, d, a = preco.get(home), preco.get("Draw"), preco.get(away)
            if h and d and a:
                return float(h), float(d), float(a)
    return None


def _evento_existente(con, casa_id, fora_id, ts, janela_h=36):
    row = con.execute(
        "SELECT id FROM evento WHERE casa_id=? AND fora_id=? AND ABS(inicio_ts-?)<=? "
        "ORDER BY (id < 0), ABS(inicio_ts-?) LIMIT 1",
        (casa_id, fora_id, ts, janela_h * 3600, ts)).fetchone()
    return row[0] if row else None


def _neg(*parts):
    import hashlib
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return -(int(h[:15], 16) & ((1 << 62) - 1)) - 1


def coletar_sport(con, mtch, key, sport_key, regions="eu"):
    url = f"{BASE}/sports/{sport_key}/odds"
    r = requests.get(url, params={"apiKey": key, "regions": regions,
                                  "markets": "h2h", "oddsFormat": "decimal"}, timeout=40)
    rem = r.headers.get("x-requests-remaining")
    if r.status_code != 200:
        print(f"[{sport_key}] HTTP {r.status_code}: {r.text[:160]}")
        return 0, 0, rem
    eventos = r.json()
    n_ev = n_od = 0
    for ev in eventos:
        ts = _ts(ev.get("commence_time"))
        if ts is None:
            continue
        casa_id = mtch.resolver(FONTE, ev.get("home_team"))
        fora_id = mtch.resolver(FONTE, ev.get("away_team"))
        if casa_id is None or fora_id is None:
            continue
        trio = _1x2_do_evento(ev)
        existente = _evento_existente(con, casa_id, fora_id, ts)
        if existente is not None and existente > 0:
            eid = existente
        else:
            eid = existente if existente is not None else _neg(FONTE, sport_key, ts, casa_id, fora_id)
            agora = int(dt.datetime.now(dt.timezone.utc).timestamp())
            gravar.gravar_evento_externo(con, {
                "id": eid, "torneio_id": _neg(FONTE, sport_key),
                "torneio_nome": sport_key.replace("soccer_", "").replace("_", " ").title(),
                "temporada_id": None, "casa_id": casa_id, "fora_id": fora_id,
                "inicio_ts": ts, "status": "finished" if ts < agora else "notstarted",
            })
            n_ev += 1
        if trio:
            for esc, dec in zip(("1", "X", "2"), trio):
                n_od += gravar.gravar_odd_linha(con, eid, "Full time", esc, dec)
    con.commit()
    print(f"[{sport_key}] {len(eventos)} jogos, +{n_ev} eventos, {n_od} odds  (créditos restantes: {rem})")
    return n_ev, n_od, rem


def _ativos(key):
    """Conjunto de sport_keys ATIVAS (em temporada) — /sports sem all=true NÃO gasta crédito."""
    try:
        r = requests.get(f"{BASE}/sports", params={"apiKey": key}, timeout=40)
        if r.status_code == 200:
            return {s["key"] for s in r.json()}
    except Exception:
        pass
    return None


def listar(key):
    r = requests.get(f"{BASE}/sports", params={"apiKey": key, "all": "true"}, timeout=40)
    if r.status_code != 200:
        sys.exit(f"erro ao listar: HTTP {r.status_code} {r.text[:160]}")
    socc = [s for s in r.json() if s.get("group") == "Soccer"]
    print(f"{len(socc)} ligas de futebol disponíveis:")
    for s in sorted(socc, key=lambda x: x.get("key", "")):
        ativo = "" if s.get("active") else " (inativa)"
        print(f"  {s['key']:<42} {s.get('title','')}{ativo}")


def main():
    ap = argparse.ArgumentParser(description="Coletor the-odds-api (odds 1X2 correntes).")
    ap.add_argument("--sports", help="sport_keys separadas por vírgula (override; senão = todas ativas)")
    ap.add_argument("--regions", default="eu", help="região (eu/uk/us); 1 região = 1 crédito")
    ap.add_argument("--max", type=int, default=16,
                    help="máx de ligas/dia (protege o plano grátis ~500 créditos/mês). default 16")
    ap.add_argument("--dry", action="store_true", help="só lista as ligas que puxaria (não gasta crédito)")
    ap.add_argument("--listar", action="store_true", help="lista TODAS as ligas de futebol (não gasta crédito)")
    a = ap.parse_args()

    key = _api_key()
    if not key:
        sys.exit("THE_ODDS_API_KEY ausente. Cadastre grátis em https://the-odds-api.com "
                 "e exporte a variável de ambiente ou crie um .env no projeto.")
    if a.listar:
        listar(key)
        return
    if not os.path.exists(DB):
        sys.exit("futebol.db não existe.")

    sports_arg = [s.strip() for s in a.sports.split(",")] if a.sports else None
    ativos = _ativos(key)                    # ligas em temporada (auto-ajusta); /sports é grátis
    if sports_arg:                           # usuário pediu ligas específicas
        sports = sports_arg
        if ativos is not None:
            fora = [s for s in sports if s not in ativos]
            sports = [s for s in sports if s in ativos]
            if fora:
                print(f"(fora de temporada, pulando: {', '.join(fora)})")
    elif ativos is None:                     # sem /sports: cai no padrão seguro
        sports = DEFAULT_SPORTS
        print("(/sports indisponível; usando lista padrão Brasil/Libertadores)")
    else:                                    # default: TODAS as ligas ativas, grandes primeiro, com teto
        # exclui mercados de aposta-futura (campeão etc.) — não têm 1X2 (h2h)
        soccer = [s for s in ativos if s.startswith("soccer_") and "winner" not in s]
        sports = _priorizar(soccer)
        if len(sports) > a.max:
            cortadas = sports[a.max:]
            sports = sports[:a.max]
            print(f"(teto de {a.max} ligas/dia p/ caber no plano grátis — fora hoje: "
                  f"{', '.join(c.replace('soccer_', '') for c in cortadas)})")
    if not sports:
        print("Nenhuma liga ativa no momento — nada a coletar.")
        return
    if a.dry:                                # só mostra o que puxaria (não gasta crédito)
        print(f"Puxaria {len(sports)} ligas hoje (~{len(sports)} créditos):")
        for s in sports:
            print("  ", s)
        return
    con = conectar()
    mtch = mapeamento_times.Matcher(con)
    try:
        for sk in sports:
            try:
                _, _, rem = coletar_sport(con, mtch, key, sk, a.regions)
                if rem is not None and str(rem).isdigit() and int(rem) < 5:
                    print(f"(crédito quase esgotado: {rem} restantes — parando por hoje)")
                    break
            except Exception as e:
                print(f"[{sk}] erro: {e}")
    finally:
        con.commit()
        print("\nmapeamento de times:", mtch.resumo())
        con.close()


if __name__ == "__main__":
    main()
