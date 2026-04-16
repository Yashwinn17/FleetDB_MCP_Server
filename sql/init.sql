-- FleetDB schema
-- Domain: fleet / mobility operations
-- Created for the FleetDB MCP Server

-- ============================================================
-- CORE TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS drivers (
    driver_id       SERIAL PRIMARY KEY,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    license_number  TEXT UNIQUE NOT NULL,
    license_expiry  DATE NOT NULL,
    hired_at        DATE NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id      SERIAL PRIMARY KEY,
    vin             TEXT UNIQUE NOT NULL,
    make            TEXT NOT NULL,
    model           TEXT NOT NULL,
    year            INTEGER NOT NULL CHECK (year BETWEEN 1990 AND 2030),
    fuel_type       TEXT NOT NULL CHECK (fuel_type IN ('gasoline','diesel','electric','hybrid')),
    odometer_km     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','maintenance','retired','lost')),
    acquired_at     DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS maintenance_events (
    event_id        SERIAL PRIMARY KEY,
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
    event_date      DATE NOT NULL,
    event_type      TEXT NOT NULL
                    CHECK (event_type IN ('oil_change','tire_rotation','brake_service',
                                          'battery_replacement','inspection','repair','recall')),
    cost_usd        NUMERIC(10,2) NOT NULL CHECK (cost_usd >= 0),
    downtime_hours  NUMERIC(6,2) NOT NULL DEFAULT 0 CHECK (downtime_hours >= 0),
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id         SERIAL PRIMARY KEY,
    vehicle_id      INTEGER NOT NULL REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
    driver_id       INTEGER NOT NULL REFERENCES drivers(driver_id) ON DELETE RESTRICT,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    distance_km     NUMERIC(8,2),
    fuel_used_l     NUMERIC(6,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_maint_vehicle  ON maintenance_events(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_maint_date     ON maintenance_events(event_date);
CREATE INDEX IF NOT EXISTS idx_trips_vehicle  ON trips(vehicle_id);
CREATE INDEX IF NOT EXISTS idx_trips_driver   ON trips(driver_id);
CREATE INDEX IF NOT EXISTS idx_trips_started  ON trips(started_at);

-- ============================================================
-- AUDIT LOG  (written by every confirmed write tool call)
-- ============================================================

CREATE TABLE IF NOT EXISTS mcp_audit_log (
    audit_id        BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor           TEXT NOT NULL,              -- client identifier, e.g. "claude-desktop"
    tool_name       TEXT NOT NULL,              -- which MCP tool was invoked
    proposal_id     UUID,                       -- links to the two-phase proposal, if any
    sql_text        TEXT NOT NULL,              -- the actual SQL executed
    reason          TEXT,                       -- human-readable justification from client
    rows_affected   INTEGER,
    success         BOOLEAN NOT NULL,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_occurred ON mcp_audit_log(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor    ON mcp_audit_log(actor);
