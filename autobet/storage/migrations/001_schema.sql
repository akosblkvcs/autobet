CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject       text UNIQUE,
    email         text,
    role          text        NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    status        text        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE channels (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chat_id    bigint      NOT NULL UNIQUE,
    title      text        NOT NULL,
    enabled    boolean     NOT NULL DEFAULT true,
    forced     boolean     NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE messages (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id  bigint      NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    external_id text        NOT NULL UNIQUE,
    sent_at     timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    text        text        NOT NULL DEFAULT '',
    media_path  text
);

CREATE INDEX messages_received_at_idx ON messages (received_at DESC);

CREATE TABLE tips (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id    bigint      NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    combined_odds numeric(10, 3),
    model         text        NOT NULL DEFAULT '',
    vision_ms     integer,
    raw           jsonb,
    parsed_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tip_legs (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tip_id    bigint  NOT NULL REFERENCES tips (id) ON DELETE CASCADE,
    position  integer NOT NULL,
    sport     text    NOT NULL DEFAULT '',
    event     text    NOT NULL,
    market    text    NOT NULL,
    selection text    NOT NULL,
    odds      numeric(10, 3),

    UNIQUE (tip_id, position)
);

CREATE TABLE selections (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tip_leg_id      bigint      NOT NULL UNIQUE REFERENCES tip_legs (id) ON DELETE CASCADE,
    status          text        NOT NULL CHECK (status IN (
                        'resolved', 'no_event', 'no_market', 'no_outcome',
                        'no_odds', 'ambiguous')),
    event_id        text,
    event_name      text,
    market_id       text,
    outcome_id      text,
    betting_type_id text,
    offer_id        text,
    odds            numeric(10, 3),
    starts_at       timestamptz,
    resolved_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bets (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tip_id         bigint      NOT NULL REFERENCES tips (id) ON DELETE CASCADE,
    user_id        bigint      REFERENCES users (id) ON DELETE SET NULL,
    state          text        NOT NULL CHECK (state IN ('placed', 'refused', 'error')),
    refusal_code   text        CHECK (refusal_code IN (
                       'leg_unresolved', 'event_started', 'odds_drop', 'odds_rise',
                       'horizon', 'insufficient_balance', 'user_paused',
                       'daily_loss_limit', 'stake_too_small', 'paper_mode',
                       'dry_run', 'book_rejected')),
    refusal_detail text        NOT NULL DEFAULT '',
    stake          numeric(12, 2) NOT NULL,
    odds           numeric(10, 3),
    reference      text        NOT NULL DEFAULT '',
    placed_at      timestamptz NOT NULL,
    settlement     text        CHECK (settlement IN (
                       'pending', 'won', 'lost', 'void', 'half_won', 'half_lost',
                       'cashed_out')),
    returned       numeric(12, 2),
    settled_at     timestamptz,

    UNIQUE (tip_id, user_id)
);

CREATE UNIQUE INDEX bets_unowned_tip_idx ON bets (tip_id) WHERE user_id IS NULL;
CREATE INDEX bets_placed_at_idx ON bets (placed_at DESC);
CREATE INDEX bets_user_idx ON bets (user_id);

CREATE TABLE bet_legs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bet_id       bigint NOT NULL REFERENCES bets (id) ON DELETE CASCADE,
    tip_leg_id   bigint NOT NULL REFERENCES tip_legs (id) ON DELETE CASCADE,
    selection_id bigint REFERENCES selections (id) ON DELETE SET NULL,
    odds         numeric(10, 3),
    settlement   text   CHECK (settlement IN (
                     'pending', 'won', 'lost', 'void', 'half_won', 'half_lost')),

    UNIQUE (bet_id, tip_leg_id)
);

CREATE TABLE tournaments (
    id         text PRIMARY KEY,
    upcoming   integer     NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE events (
    id            text PRIMARY KEY,
    tournament_id text        NOT NULL REFERENCES tournaments (id) ON DELETE CASCADE,
    name          text        NOT NULL,
    sport         text        NOT NULL,
    home_id       text        NOT NULL,
    away_id       text        NOT NULL,
    home          text[]      NOT NULL,
    away          text[]      NOT NULL,
    starts_at     timestamptz,
    indexed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX events_sport_idx ON events (sport);
CREATE INDEX events_tournament_idx ON events (tournament_id);

CREATE TABLE settings (
    key        text PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by bigint REFERENCES users (id) ON DELETE SET NULL
);

CREATE TABLE audit_log (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    actor_id  bigint REFERENCES users (id) ON DELETE SET NULL,
    action    text        NOT NULL,
    entity    text        NOT NULL DEFAULT '',
    entity_id text        NOT NULL DEFAULT '',
    detail    jsonb
);

CREATE INDEX audit_log_at_idx ON audit_log (at DESC);
