CREATE TABLE user_settings (
    user_id               bigint PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    mode                  text NOT NULL DEFAULT 'paper'
                              CHECK (mode IN ('paper', 'live')),
    paused                boolean NOT NULL DEFAULT false,
    stake                 numeric(12, 2),
    max_odds_drop_percent double precision,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);
