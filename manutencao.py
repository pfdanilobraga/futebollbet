# -*- coding: utf-8 -*-
"""
Ciclo de manutenção diário (para rodar no Agendador de Tarefas do Windows).

Faz, de forma gentil (respeitando o rate-limit do coletor):
  1. Coleta os jogos de hoje e de ontem (entram novas partidas/resultados).
  2. Atualiza a classificação de cada temporada que já está no banco.
  3. Faz backfill LIMITADO de detalhes (escalações/xG/odds) dos encerrados —
     N jogos por execução, então a base histórica vai se completando ao longo
     dos dias sem martelar o servidor (importante enquanto o Cloudflare está sensível).
  4. Re-treina o modelo de ML (se houver scikit-learn/xgboost) e prevê os próximos jogos.

Tudo é idempotente: pode rodar quantas vezes quiser.

Uso:
  py manutencao.py                 # ciclo padrão (backfill de 40 jogos)
  py manutencao.py --backfill 100  # backfill maior nesta execução
"""
import argparse
import datetime as dt
import os
import sqlite3
import subprocess
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PASTA = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(PASTA, "futebol.db")
PY = sys.executable


def _conectar():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def main(backfill):
    if not os.path.exists(DB):
        sys.exit("futebol.db não existe — rode o coletor de uma temporada antes.")

    import coletor
    con = coletor.conectar()
    try:
        hoje = dt.date.today()
        print(f"=== Manutenção {hoje} ===")

        # 1) jogos de ontem e hoje (resultados + novas partidas)
        for delta in (-1, 0):
            coletor.coletar_dia(con, (hoje + dt.timedelta(days=delta)).isoformat())

        # 2) classificação de cada (torneio, temporada) presente no banco
        pares = con.execute(
            "SELECT DISTINCT torneio_id, temporada_id FROM evento "
            "WHERE torneio_id IS NOT NULL AND temporada_id IS NOT NULL").fetchall()
        for ut_id, season_id in pares:
            coletor.coletar_classificacao(con, ut_id, season_id)

    finally:
        con.close()
        coletor.transporte.fechar()

    # 3) backfill de detalhes via RASPADOR (navegador real — contorna o Cloudflare).
    #    Roda headless, gravando estatísticas/escalações/xG/odds dos encerrados.
    try:
        import raspador
        print(f"\n--- Raspando detalhes de até {backfill} jogos (navegador) ---")
        raspador.backfill(limite=backfill, headless=True)
    except Exception as e:
        print(f"(raspador indisponível: {e})")

    # 3.5) odds + resultados de muitas ligas via football-data.co.uk (CSV, sem
    #      Cloudflare). Só temporada corrente+anterior — barato, atualiza 2x/semana.
    print("\n--- Odds/resultados football-data.co.uk (temporada corrente) ---")
    subprocess.run([PY, os.path.join(PASTA, "coletor_oddscsv.py"), "--corrente"], check=False)

    # 3.6) odds CORRENTES de jogos FUTUROS via the-odds-api (só ligas ativas, ~poucos
    #      créditos/dia). Requer THE_ODDS_API_KEY no .env; sem ela o passo só avisa e segue.
    print("\n--- Odds correntes the-odds-api (jogos futuros) ---")
    subprocess.run([PY, os.path.join(PASTA, "coletor_oddsapi.py")], check=False)

    # 4) re-treino + previsões (silencioso se faltar lib de ML)
    try:
        import sklearn, xgboost  # noqa: F401
        print("\n--- Re-treinando modelo de ML ---")
        subprocess.run([PY, os.path.join(PASTA, "treino.py")], check=False)
        print("\n--- Prevendo próximos jogos ---")
        subprocess.run([PY, os.path.join(PASTA, "prever.py"), "--proximos"], check=False)
    except ImportError:
        print("\n(scikit-learn/xgboost não instalados — pulei o ML. "
              "Instale com: py -m pip install scikit-learn xgboost joblib pandas)")

    # modelos estatísticos sempre rodam
    print("\n--- Probabilidades estatísticas (odds + Poisson) ---")
    subprocess.run([PY, os.path.join(PASTA, "probabilidades.py"), "proximos"], check=False)
    print("\n=== Fim ===")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=150,
                    help="quantos jogos encerrados detalhar por execução (~15s/jogo)")
    a = ap.parse_args()
    main(a.backfill)