# -*- coding: utf-8 -*-
"""
Coletor de ODDS + RESULTADOS históricos do football-data.co.uk (grátis, CSV
estático, SEM Cloudflare — só requests). Destrava o gargalo de odds em escala
mundial e semeia `evento` (resultado) para dezenas de ligas que NÃO raspamos no
SofaScore (o "anel de largura" do plano).

Dois formatos:
  - MAIN  : mmz4281/{codigo_temporada}/{div}.csv  (ex.: 2425/E0.csv) — tem odds
            de ABERTURA (B365H) e FECHAMENTO (B365CH) + over/under + handicap.
  - EXTRA : new/{PAIS}.csv  (ex.: BRA.csv) — 1 arquivo por país, SÓ odds de
            fechamento (sufixo C), colunas Home/Away/HG/AG/Res.

Mapeia para o schema existente: `evento` (status finished, vencedor/gols) e
`odd` (mercado 'Full time' 1/X/2 e 'Match goals' 2.5 Over/Under), usando os
mesmos nomes que `features.py`/`probabilidades.py` consultam (ativam imp_*).

Times: resolvidos via mapeamento_times.Matcher (casa em time_id do SofaScore
quando há overlap; senão cria sintético negativo). Eventos: casados a um evento
SofaScore existente (±36h, mesmos times) p/ NÃO duplicar; senão id sintético.

Uso:
  py coletor_oddscsv.py --teste                  # E0 só da última temporada (smoke)
  py coletor_oddscsv.py --ligas E0,SP1,D1 --desde-ano 2018
  py coletor_oddscsv.py --extra BRA,ARG          # ligas "extra" (1 arquivo/país)
  py coletor_oddscsv.py --tudo --desde-ano 2015  # tudo (largura máxima)
"""
import argparse
import datetime as dt
import hashlib
import io
import os
import re
import sqlite3
import sys
import time

import pandas as pd
import requests

import gravar
import mapeamento_times

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
BASE = "https://www.football-data.co.uk"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FONTE = "fdcouk"

# div -> (nome do torneio, país)
MAIN = {
    "E0": ("Premier League", "England"), "E1": ("Championship", "England"),
    "E2": ("League One", "England"), "E3": ("League Two", "England"),
    "EC": ("National League", "England"),
    "SC0": ("Scottish Premiership", "Scotland"), "SC1": ("Scottish Championship", "Scotland"),
    "SC2": ("Scottish League One", "Scotland"), "SC3": ("Scottish League Two", "Scotland"),
    "D1": ("Bundesliga", "Germany"), "D2": ("2. Bundesliga", "Germany"),
    "I1": ("Serie A", "Italy"), "I2": ("Serie B", "Italy"),
    "SP1": ("La Liga", "Spain"), "SP2": ("La Liga 2", "Spain"),
    "F1": ("Ligue 1", "France"), "F2": ("Ligue 2", "France"),
    "N1": ("Eredivisie", "Netherlands"), "B1": ("Jupiler Pro League", "Belgium"),
    "P1": ("Primeira Liga", "Portugal"), "T1": ("Super Lig", "Turkey"),
    "G1": ("Super League Greece", "Greece"),
}
# código do país no formato new/{code}.csv -> nome do país
EXTRA = {
    "ARG": "Argentina", "AUT": "Austria", "BRA": "Brazil", "CHN": "China",
    "DNK": "Denmark", "FIN": "Finland", "IRL": "Ireland", "JPN": "Japan",
    "MEX": "Mexico", "NOR": "Norway", "POL": "Poland", "ROU": "Romania",
    "RUS": "Russia", "SWE": "Sweden", "SWZ": "Switzerland", "USA": "USA",
}

# preferência de casa de aposta (1X2). Bet365 primeiro (é a casa do projeto),
# depois Pinnacle (sharp), William Hill, média e máximo do mercado.
CLOSE_1X2 = ["B365C", "PSC", "WHC", "AvgC", "MaxC", "BFEC"]
OPEN_1X2 = ["B365", "PS", "WH", "Avg", "Max", "BFE"]
CLOSE_OU = ["B365C", "PC", "AvgC", "MaxC"]
OPEN_OU = ["B365", "P", "Avg", "Max"]
VENC = {"H": 1, "A": 2, "D": 3}


def conectar():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _neg(*parts):
    """id NEGATIVO determinístico a partir das partes (estável entre execuções)."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return -(int(h[:15], 16) & ((1 << 62) - 1)) - 1


def _num(v):
    """float > 1.0 (odd decimal válida) ou None."""
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def _ts(date_str, time_str):
    """(dd/mm/yyyy[, HH:MM]) -> unix ts (UTC, p/ ordenação consistente)."""
    s = str(date_str).strip()
    if not s or s.lower() in ("nan", "nat"):
        return None
    d = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            d = dt.datetime.strptime(s, fmt)
            break
        except ValueError:
            pass
    if d is None:
        return None
    hh, mm = 12, 0
    m = re.match(r"^(\d{1,2}):(\d{2})", str(time_str).strip())
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    return int(d.replace(hour=hh, minute=mm, tzinfo=dt.timezone.utc).timestamp())


def _trio(row, prefixes):
    """1X2: primeiro prefixo (casa de aposta) com H/D/A completos -> [h, d, a]."""
    for p in prefixes:
        vals = [_num(row.get(p + s)) for s in ("H", "D", "A")]
        if all(v is not None for v in vals):
            return vals
    return None


def _par_ou(row, prefixes):
    """Over/Under 2.5: primeiro prefixo com >2.5 e <2.5 -> [over, under]."""
    for p in prefixes:
        o, u = _num(row.get(p + ">2.5")), _num(row.get(p + "<2.5"))
        if o is not None and u is not None:
            return [o, u]
    return None


def _baixar(url):
    for tentativa in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
        except Exception as e:
            print(f"  ! rede {url}: {e}")
            return None
        if r.status_code == 200:
            break
        if r.status_code in (429, 503) and tentativa == 0:
            time.sleep(3)
            continue
        print(f"  ! {url} -> HTTP {r.status_code}")
        return None
    time.sleep(0.3)   # educado com o servidor (evita 429 em varredura grande)
    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(r.content), encoding=enc, on_bad_lines="skip")
            df.columns = [str(c).replace("﻿", "").strip() for c in df.columns]
            return df
        except Exception:
            continue
    print(f"  ! falha ao parsear {url}")
    return None


def _evento_existente(con, casa_id, fora_id, ts, janela_h=36):
    """Acha um evento já no banco com mesmos times no intervalo de tempo.
    Prefere id POSITIVO (SofaScore) sobre sintético, p/ anexar odds sem duplicar."""
    row = con.execute(
        "SELECT id FROM evento WHERE casa_id=? AND fora_id=? AND ABS(inicio_ts-?)<=? "
        "ORDER BY (id < 0), ABS(inicio_ts-?) LIMIT 1",
        (casa_id, fora_id, ts, janela_h * 3600, ts)).fetchone()
    return row[0] if row else None


def _odds_da_linha(row, tem_abertura):
    """Constrói a lista de odds (mercado, escolha, dec, abe, parametro) da linha."""
    odds = []
    close = _trio(row, CLOSE_1X2)
    opn = _trio(row, OPEN_1X2) if tem_abertura else None
    for i, esc in enumerate(("1", "X", "2")):
        dec = close[i] if close else (opn[i] if opn else None)
        abe = opn[i] if opn else None
        if dec:
            odds.append(("Full time", "", esc, dec, abe))
    clo = _par_ou(row, CLOSE_OU)
    opo = _par_ou(row, OPEN_OU) if tem_abertura else None
    for j, esc in enumerate(("Over", "Under")):
        dec = clo[j] if clo else (opo[j] if opo else None)
        abe = opo[j] if opo else None
        if dec:
            odds.append(("Match goals", "2.5", esc, dec, abe))
    return odds


def _gravar_partida(con, mtch, *, pais, torneio_id, torneio_nome,
                    temporada_id, temporada_nome, ano,
                    home, away, ts, gc, ga, gc1, ga1, res, odds):
    """Resolve times, casa/cria o evento e grava as odds. Retorna (eventos, odds)."""
    if not home or not away or ts is None:
        return 0, 0
    casa_id = mtch.resolver(FONTE, home, pais)
    fora_id = mtch.resolver(FONTE, away, pais)
    if casa_id is None or fora_id is None:
        return 0, 0

    existente = _evento_existente(con, casa_id, fora_id, ts)
    n_ev = 0
    if existente is not None and existente > 0:
        eid = existente                     # evento SofaScore — NÃO sobrescreve
    else:
        eid = existente if existente is not None else _neg(FONTE, torneio_id, ts, home, away)
        gravar.gravar_evento_externo(con, {
            "id": eid, "torneio_id": torneio_id, "torneio_nome": torneio_nome,
            "pais": pais, "temporada_id": temporada_id,
            "temporada_nome": temporada_nome, "ano": ano,
            "casa_id": casa_id, "fora_id": fora_id, "inicio_ts": ts,
            "status": "finished", "vencedor": VENC.get(res),
            "gols_casa": gc, "gols_fora": ga,
            "gols_casa_1t": gc1, "gols_fora_1t": ga1,
        })
        n_ev = 1
    n_od = 0
    for mercado, parametro, esc, dec, abe in odds:
        n_od += gravar.gravar_odd_linha(con, eid, mercado, esc, dec, abe, parametro)
    return n_ev, n_od


def coletar_main(con, mtch, div, codigos, desde_ano):
    nome, pais = MAIN[div]
    torneio_id = _neg(FONTE, div)
    tot_ev = tot_od = tot_lin = 0
    for code in codigos:
        df = _baixar(f"{BASE}/mmz4281/{code}/{div}.csv")
        if df is None or df.empty:
            continue
        temporada_id = _neg(FONTE, div, code)
        ano = f"20{code[:2]}/20{code[2:]}"
        for row in df.to_dict("records"):
            ts = _ts(row.get("Date"), row.get("Time"))
            if ts is None or dt.datetime.fromtimestamp(ts, dt.timezone.utc).year < desde_ano:
                continue
            res = row.get("FTR")
            if res not in VENC:
                continue
            tot_lin += 1
            ev, od = _gravar_partida(
                con, mtch, pais=pais, torneio_id=torneio_id, torneio_nome=nome,
                temporada_id=temporada_id, temporada_nome=f"{nome} {ano}", ano=ano,
                home=row.get("HomeTeam"), away=row.get("AwayTeam"), ts=ts,
                gc=_int(row.get("FTHG")), ga=_int(row.get("FTAG")),
                gc1=_int(row.get("HTHG")), ga1=_int(row.get("HTAG")),
                res=res, odds=_odds_da_linha(row, tem_abertura=True))
            tot_ev += ev
            tot_od += od
        con.commit()
        print(f"  {div} {code}: {len(df)} linhas")
    print(f"[{div}] {nome}: {tot_lin} partidas, {tot_ev} gravadas/atualizadas, {tot_od} odds")
    return tot_ev, tot_od


def coletar_extra(con, mtch, code, desde_ano):
    pais = EXTRA[code]
    df = _baixar(f"{BASE}/new/{code}.csv")
    if df is None or df.empty:
        print(f"[{code}] vazio/erro")
        return 0, 0
    tot_ev = tot_od = tot_lin = 0
    for row in df.to_dict("records"):
        ts = _ts(row.get("Date"), row.get("Time"))
        if ts is None or dt.datetime.fromtimestamp(ts, dt.timezone.utc).year < desde_ano:
            continue
        res = row.get("Res")
        if res not in VENC:
            continue
        liga = str(row.get("League") or pais).strip()
        season = str(row.get("Season") or "").strip()
        torneio_id = _neg(FONTE, code, liga)
        temporada_id = _neg(FONTE, code, liga, season)
        tot_lin += 1
        ev, od = _gravar_partida(
            con, mtch, pais=pais, torneio_id=torneio_id,
            torneio_nome=f"{pais} - {liga}", temporada_id=temporada_id,
            temporada_nome=f"{liga} {season}", ano=season,
            home=row.get("Home"), away=row.get("Away"), ts=ts,
            gc=_int(row.get("HG")), ga=_int(row.get("AG")),
            gc1=None, ga1=None,
            res=res, odds=_odds_da_linha(row, tem_abertura=False))
        tot_ev += ev
        tot_od += od
    con.commit()
    print(f"[{code}] {pais}: {tot_lin} partidas, {tot_ev} gravadas/atualizadas, {tot_od} odds")
    return tot_ev, tot_od


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _ano_inicio_temporada_corrente():
    """Ano em que a temporada europeia corrente começou (jul-jun)."""
    hoje = dt.date.today()
    return hoje.year if hoje.month >= 7 else hoje.year - 1


def _codigos_temporada(desde_ano):
    """Gera ['1819','1920',...,'2526'] do ano inicial até a temporada corrente."""
    fim = _ano_inicio_temporada_corrente()
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(desde_ano, fim + 1)]


def coletar_corrente(con, mtch):
    """Modo manutenção: só temporada corrente (+ anterior, p/ resultados tardios)
    de TODAS as ligas. Barato p/ rodar diário (football-data atualiza 2x/semana)."""
    ini = _ano_inicio_temporada_corrente() - 1
    codigos = _codigos_temporada(ini)
    for div in MAIN:
        coletar_main(con, mtch, div, codigos, ini)
    for code in EXTRA:
        coletar_extra(con, mtch, code, ini)


def main():
    ap = argparse.ArgumentParser(description="Coletor football-data.co.uk (odds+resultados).")
    ap.add_argument("--ligas", help="divs MAIN separadas por vírgula (ex.: E0,SP1,D1)")
    ap.add_argument("--extra", help="países EXTRA separados por vírgula (ex.: BRA,ARG)")
    ap.add_argument("--tudo", action="store_true", help="todas as ligas MAIN + EXTRA")
    ap.add_argument("--corrente", action="store_true",
                    help="só temporada corrente+anterior de TODAS as ligas (p/ manutenção diária)")
    ap.add_argument("--teste", action="store_true", help="smoke test: E0 só da temporada corrente")
    ap.add_argument("--desde-ano", type=int, default=2015, help="ano inicial (default 2015)")
    a = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit("futebol.db não existe — rode o coletor SofaScore de uma temporada antes.")

    con = conectar()
    mtch = mapeamento_times.Matcher(con)
    try:
        if a.teste:
            y = _ano_inicio_temporada_corrente()
            corrente = f"{y % 100:02d}{(y + 1) % 100:02d}"
            coletar_main(con, mtch, "E0", [corrente], 2000)
        elif a.corrente:
            coletar_corrente(con, mtch)
        else:
            ligas = list(MAIN) if a.tudo else (
                [x.strip() for x in a.ligas.split(",")] if a.ligas else [])
            extras = list(EXTRA) if a.tudo else (
                [x.strip() for x in a.extra.split(",")] if a.extra else [])
            if not ligas and not extras:
                sys.exit("nada a coletar — use --ligas, --extra, --tudo ou --teste")
            codigos = _codigos_temporada(a.desde_ano)
            for div in ligas:
                if div in MAIN:
                    coletar_main(con, mtch, div, codigos, a.desde_ano)
                else:
                    print(f"(div desconhecida: {div})")
            for code in extras:
                if code in EXTRA:
                    coletar_extra(con, mtch, code, a.desde_ano)
                else:
                    print(f"(país extra desconhecido: {code})")
    finally:
        con.commit()
        print("\nmapeamento de times:", mtch.resumo())
        con.close()


if __name__ == "__main__":
    main()
