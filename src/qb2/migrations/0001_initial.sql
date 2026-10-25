-- Quantum Brain v2 research database, migration 0001
-- Raw ticks are retained as received; cleaning happens at read time so every
-- dataset can be rebuilt from the same raw data.

CREATE TABLE IF NOT EXISTS instruments (
    symbol          TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    market          TEXT,
    submarket       TEXT,
    pip_size        REAL,
    is_open         INTEGER,
    rise_fall_5m    INTEGER,          -- NULL = not checked, 0/1 from contracts_for
    notes           TEXT,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,    -- backfill, stream, simulated, import
    symbol          TEXT,
    params          TEXT,             -- JSON
    source          TEXT NOT NULL,    -- deriv_api, simulator, file
    started_at      REAL NOT NULL,
    finished_at     REAL,
    status          TEXT NOT NULL DEFAULT 'running',
    rows_inserted   INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS ticks (
    symbol          TEXT NOT NULL,
    epoch           INTEGER NOT NULL,
    quote           REAL NOT NULL,
    run_id          INTEGER REFERENCES ingestion_runs(id),
    PRIMARY KEY (symbol, epoch)
);

-- Every proposal received (bought or not). This is how a historical record of
-- real contract prices and payouts accumulates for the backtester.
CREATE TABLE IF NOT EXISTS contract_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    contract_type   TEXT NOT NULL,
    stake           REAL NOT NULL,
    payout          REAL NOT NULL,
    duration_seconds INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    quote_time      REAL NOT NULL,
    spot            REAL,
    spot_time       INTEGER,
    proposal_id     TEXT,
    source          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_symbol_time ON contract_quotes(symbol, quote_time);

CREATE TABLE IF NOT EXISTS data_quality_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    created_at      REAL NOT NULL,
    passed          INTEGER NOT NULL,
    report          TEXT NOT NULL     -- JSON
);

CREATE TABLE IF NOT EXISTS models (
    version         TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    model_type      TEXT NOT NULL,
    status          TEXT NOT NULL,    -- candidate, validated, rejected, champion, retired
    feature_version TEXT NOT NULL,
    dataset_hash    TEXT NOT NULL,
    artifact_path   TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL,
    metrics         TEXT NOT NULL,    -- JSON, chronological out-of-sample only
    created_at      REAL NOT NULL,
    approved_by     TEXT,
    approved_at     REAL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_models_symbol_status ON models(symbol, status);

CREATE TABLE IF NOT EXISTS holdout_uses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_hash    TEXT NOT NULL,
    model_version   TEXT,
    used_at         REAL NOT NULL,
    purpose         TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    decided_at      REAL NOT NULL,
    mode            TEXT NOT NULL,    -- paper, demo, backtest
    action          TEXT NOT NULL,    -- TRADE, NO_TRADE, HALTED
    contract_type   TEXT,
    model_version   TEXT,
    payload         TEXT NOT NULL     -- JSON (probabilities, EV, reasons, assumptions)
);
CREATE INDEX IF NOT EXISTS idx_decisions_time ON decisions(decided_at);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,    -- paper or demo (never live)
    symbol          TEXT NOT NULL,
    contract_type   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    stake           REAL NOT NULL,
    payout          REAL NOT NULL,
    start_time      REAL NOT NULL,
    expiry_time     REAL NOT NULL,
    status          TEXT NOT NULL,    -- open, won, lost, void
    entry_spot      REAL,
    exit_spot       REAL,
    profit          REAL,
    contract_id     TEXT,
    model_version   TEXT,
    decision_id     INTEGER REFERENCES decisions(id),
    settled_at      REAL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    severity        TEXT NOT NULL,    -- info, warning, critical
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL     -- JSON
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(created_at);

CREATE TABLE IF NOT EXISTS drift_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL NOT NULL,
    symbol          TEXT NOT NULL,
    model_version   TEXT,
    status          TEXT NOT NULL,    -- OK, WARN, HALT
    report          TEXT NOT NULL     -- JSON
);

CREATE TABLE IF NOT EXISTS kv_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,    -- JSON
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS research_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,    -- instrument_ranking, patterns, backtest, validation
    created_at      REAL NOT NULL,
    payload         TEXT NOT NULL     -- JSON
);
