-- ============================================================
-- futebol.db — Banco de dados de partidas (fonte: SofaScore API)
-- SQLite. Para Postgres: trocar INTEGER PRIMARY KEY por BIGSERIAL/identity.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- Dimensões ----------

CREATE TABLE IF NOT EXISTS torneio (
    id          INTEGER PRIMARY KEY,          -- unique-tournament id (ex.: 390 Série B)
    nome        TEXT NOT NULL,
    slug        TEXT,
    pais        TEXT
);

CREATE TABLE IF NOT EXISTS temporada (
    id          INTEGER PRIMARY KEY,          -- season id (ex.: 89840)
    torneio_id  INTEGER NOT NULL REFERENCES torneio(id),
    nome        TEXT,                         -- "Brasileirão Série B 2026"
    ano         TEXT
);

CREATE TABLE IF NOT EXISTS time (
    id          INTEGER PRIMARY KEY,          -- team id (ex.: 2001 Ceará)
    nome        TEXT NOT NULL,
    nome_curto  TEXT,
    slug        TEXT,
    pais        TEXT
);

CREATE TABLE IF NOT EXISTS jogador (
    id          INTEGER PRIMARY KEY,
    nome        TEXT NOT NULL,
    posicao     TEXT,                         -- F, M, D, G
    data_nasc   INTEGER                       -- unix timestamp
);

-- ---------- Fatos: partida ----------

CREATE TABLE IF NOT EXISTS evento (
    id              INTEGER PRIMARY KEY,      -- event id
    custom_id       TEXT,                     -- id alfanumérico (usado no h2h)
    temporada_id    INTEGER REFERENCES temporada(id),
    torneio_id      INTEGER REFERENCES torneio(id),
    rodada          INTEGER,
    casa_id         INTEGER NOT NULL REFERENCES time(id),
    fora_id         INTEGER NOT NULL REFERENCES time(id),
    inicio_ts       INTEGER NOT NULL,         -- unix timestamp
    status          TEXT NOT NULL,            -- notstarted | inprogress | finished | postponed...
    vencedor        INTEGER,                  -- 1 casa, 2 fora, 3 empate (winnerCode)
    gols_casa       INTEGER,
    gols_fora       INTEGER,
    gols_casa_1t    INTEGER,
    gols_fora_1t    INTEGER,
    tem_xg          INTEGER DEFAULT 0,        -- hasXg
    arbitro         TEXT,
    estadio         TEXT,
    atualizado_em   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_evento_data    ON evento(inicio_ts);
CREATE INDEX IF NOT EXISTS ix_evento_casa    ON evento(casa_id, inicio_ts);
CREATE INDEX IF NOT EXISTS ix_evento_fora    ON evento(fora_id, inicio_ts);
CREATE INDEX IF NOT EXISTS ix_evento_temp    ON evento(temporada_id, rodada);

-- Estatísticas da partida (1 linha por evento/período/estatística)
CREATE TABLE IF NOT EXISTS evento_estatistica (
    evento_id   INTEGER NOT NULL REFERENCES evento(id),
    periodo     TEXT NOT NULL,                -- ALL | 1ST | 2ND
    grupo       TEXT NOT NULL,                -- Match overview, Shots, Attack...
    nome        TEXT NOT NULL,                -- Ball possession, Expected goals...
    casa_valor  REAL,                         -- homeValue numérico
    fora_valor  REAL,
    casa_texto  TEXT,                         -- valor exibido ("56%")
    fora_texto  TEXT,
    PRIMARY KEY (evento_id, periodo, grupo, nome)
);

-- Chutes individuais com xG (shotmap)
CREATE TABLE IF NOT EXISTS chute (
    id          INTEGER PRIMARY KEY,          -- shot id da API
    evento_id   INTEGER NOT NULL REFERENCES evento(id),
    jogador_id  INTEGER REFERENCES jogador(id),
    eh_casa     INTEGER NOT NULL,             -- isHome
    minuto      INTEGER,
    tipo        TEXT,                         -- goal | save | miss | block | post
    situacao    TEXT,                         -- regular, corner, set-piece, penalty...
    parte_corpo TEXT,
    xg          REAL,
    xgot        REAL,
    x           REAL,                         -- playerCoordinates.x
    y           REAL
);
CREATE INDEX IF NOT EXISTS ix_chute_evento ON chute(evento_id);

-- Escalações + nota individual
CREATE TABLE IF NOT EXISTS escalacao (
    evento_id    INTEGER NOT NULL REFERENCES evento(id),
    jogador_id   INTEGER NOT NULL REFERENCES jogador(id),
    time_id      INTEGER NOT NULL REFERENCES time(id),
    titular      INTEGER NOT NULL,            -- NOT substitute
    posicao      TEXT,
    nota         REAL,                        -- rating SofaScore
    minutos      INTEGER,
    xa           REAL,                        -- expectedAssists
    PRIMARY KEY (evento_id, jogador_id)
);

-- Formação tática por time na partida
CREATE TABLE IF NOT EXISTS evento_formacao (
    evento_id  INTEGER NOT NULL REFERENCES evento(id),
    time_id    INTEGER NOT NULL REFERENCES time(id),
    formacao   TEXT,                          -- "4-2-3-1"
    nota_media REAL,
    PRIMARY KEY (evento_id, time_id)
);

-- Incidentes da partida (gols, cartões, substituições) — vem embutido no HTML
CREATE TABLE IF NOT EXISTS incidente (
    evento_id   INTEGER NOT NULL REFERENCES evento(id),
    ordem       INTEGER NOT NULL,             -- índice para unicidade/ordem
    minuto      INTEGER,
    acrescimo   INTEGER,                       -- addedTime
    tipo        TEXT,                          -- goal | card | substitution | period...
    detalhe     TEXT,                          -- penalty, ownGoal, yellow, red...
    eh_casa     INTEGER,                       -- 1 casa, 0 fora
    jogador     TEXT,
    jogador_id  INTEGER,
    assistente  TEXT,
    placar_casa INTEGER,                       -- placar após o lance (gols)
    placar_fora INTEGER,
    PRIMARY KEY (evento_id, ordem)
);
CREATE INDEX IF NOT EXISTS ix_incidente_evento ON incidente(evento_id);

-- ---------- Odds (mercado de apostas) ----------

CREATE TABLE IF NOT EXISTS odd (
    evento_id     INTEGER NOT NULL REFERENCES evento(id),
    mercado       TEXT NOT NULL,              -- Full time, Double chance, Match goals...
    parametro     TEXT NOT NULL DEFAULT '',   -- ex.: linha do over/under ("2.5")
    escolha       TEXT NOT NULL,              -- 1 | X | 2 | Over | Under | Yes | No
    odd_decimal   REAL NOT NULL,              -- fractionalValue convertido (+1)
    odd_abertura  REAL,                       -- initialFractionalValue convertido
    coletado_em   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (evento_id, mercado, parametro, escolha)
);

-- ---------- Contexto pré-jogo ----------

CREATE TABLE IF NOT EXISTS pre_jogo (
    evento_id        INTEGER PRIMARY KEY REFERENCES evento(id),
    casa_forma       TEXT,                    -- "W,L,D,W,W"
    fora_forma       TEXT,
    casa_nota_media  REAL,                    -- avgRating
    fora_nota_media  REAL,
    casa_posicao     INTEGER,
    fora_posicao     INTEGER,
    h2h_casa_v       INTEGER,                 -- vitórias do mandante no confronto direto
    h2h_fora_v       INTEGER,
    h2h_empates      INTEGER,
    votos_casa       INTEGER,                 -- enquete "quem vai ganhar"
    votos_empate     INTEGER,
    votos_fora       INTEGER
);

-- ---------- Classificação (snapshot por coleta) ----------

CREATE TABLE IF NOT EXISTS classificacao (
    temporada_id  INTEGER NOT NULL REFERENCES temporada(id),
    time_id       INTEGER NOT NULL REFERENCES time(id),
    tipo          TEXT NOT NULL DEFAULT 'total',  -- total | home | away
    posicao       INTEGER,
    jogos         INTEGER,
    vitorias      INTEGER,
    empates       INTEGER,
    derrotas      INTEGER,
    gols_pro      INTEGER,
    gols_contra   INTEGER,
    pontos        INTEGER,
    coletado_em   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (temporada_id, time_id, tipo)
);

-- Estatísticas agregadas do time na temporada (~120 métricas em JSON)
CREATE TABLE IF NOT EXISTS time_temporada_stats (
    time_id       INTEGER NOT NULL REFERENCES time(id),
    temporada_id  INTEGER NOT NULL REFERENCES temporada(id),
    stats_json    TEXT NOT NULL,              -- payload completo de /statistics/overall
    coletado_em   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (time_id, temporada_id)
);

-- ---------- Saída do modelo ----------

CREATE TABLE IF NOT EXISTS probabilidade (
    evento_id     INTEGER NOT NULL REFERENCES evento(id),
    modelo        TEXT NOT NULL,              -- 'odds_implicitas' | 'poisson' | 'combinado'
    p_casa        REAL NOT NULL,
    p_empate      REAL NOT NULL,
    p_fora        REAL NOT NULL,
    detalhes_json TEXT,                       -- lambdas, inputs etc.
    calculado_em  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (evento_id, modelo)
);

-- Saída de modelos de mercados de CONTAGEM (Over/Under): escanteios, chutes,
-- chutes ao gol, cartões. Genérica — 1 linha por (evento, mercado, linha, modelo).
-- O 1X2 continua na tabela `probabilidade`; aqui ficam os totais Over/Under.
CREATE TABLE IF NOT EXISTS probabilidade_mercado (
    evento_id     INTEGER NOT NULL REFERENCES evento(id),
    mercado       TEXT NOT NULL,              -- 'escanteios' | 'chutes' | 'chutes_gol' | 'cartoes'
    linha         REAL NOT NULL,              -- linha do over/under (ex.: 9.5)
    modelo        TEXT NOT NULL,              -- 'odds_implicitas' | 'poisson' | 'combinado'
    p_over        REAL NOT NULL,
    p_under       REAL NOT NULL,
    detalhes_json TEXT,                       -- lambdas, médias, odds usadas etc.
    calculado_em  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (evento_id, mercado, linha, modelo)
);
CREATE INDEX IF NOT EXISTS ix_prob_mercado ON probabilidade_mercado(evento_id, mercado);

-- Log de sinais do alertador ao vivo (bet365) — pra validar P&L/calibracao
CREATE TABLE IF NOT EXISTS sinal_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT (datetime('now')),  -- quando o GREEN disparou
    liga        TEXT,
    casa        TEXT,
    fora        TEXT,
    event_id    INTEGER,                          -- id SofaScore (do matcher), se houver
    minuto      REAL,                             -- minuto bet365 no disparo
    placar      TEXT,                             -- placar no disparo
    resultado   TEXT,                             -- CASA | EMPATE | FORA (dominante apostado)
    prob        REAL,                             -- prob do modelo
    breakeven   REAL,                             -- 1/prob
    odd_tela    REAL,                             -- odd do bet365 no disparo
    acrescimo   INTEGER,                          -- anunciado usado (ou NULL)
    -- preenchidos depois por validar_sinais.py:
    resultado_final TEXT,                         -- CASA | EMPATE | FORA real
    acertou     INTEGER,                          -- 1 se manteve o resultado apostado
    pnl         REAL                              -- lucro por 1 unidade (odd-1 se acertou, -1 se nao)
);

-- View pronta: jogos finalizados com resultado p/ treinar/validar modelos
CREATE VIEW IF NOT EXISTS v_resultados AS
SELECT e.id, e.inicio_ts, e.temporada_id, e.rodada,
       tc.nome AS casa, tf.nome AS fora,
       e.gols_casa, e.gols_fora, e.vencedor
FROM evento e
JOIN time tc ON tc.id = e.casa_id
JOIN time tf ON tf.id = e.fora_id
WHERE e.status = 'finished';
