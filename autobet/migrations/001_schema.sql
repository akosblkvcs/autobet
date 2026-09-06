CREATE TABLE messages (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id text        NOT NULL UNIQUE,
    channel     text        NOT NULL,
    sent_at     timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    text        text        NOT NULL,
    media_kind  text,
    media_path  text
);

CREATE INDEX messages_received_at_idx ON messages (received_at DESC);

CREATE TABLE tips (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id bigint      NOT NULL UNIQUE REFERENCES messages (id) ON DELETE CASCADE,
    stake      numeric(12, 2) NOT NULL,
    parsed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE legs (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tip_id    bigint  NOT NULL REFERENCES tips (id) ON DELETE CASCADE,
    position  integer NOT NULL,
    event     text    NOT NULL,
    market    text    NOT NULL,
    selection text    NOT NULL,
    odds      numeric(10, 3) NOT NULL,

    event_id        text,
    event_name      text,
    market_id       text,
    outcome_id      text,
    betting_type_id text,
    offer_id        text,
    live_odds       numeric(10, 3),

    UNIQUE (tip_id, position)
);

CREATE TABLE bets (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tip_id    bigint      NOT NULL UNIQUE REFERENCES tips (id) ON DELETE CASCADE,
    placed_at timestamptz NOT NULL,
    reference text        NOT NULL DEFAULT '',
    refusal   text        NOT NULL DEFAULT '',
    stake     numeric(12, 2) NOT NULL,
    odds      numeric(10, 3) NOT NULL
);

CREATE INDEX bets_placed_at_idx ON bets (placed_at DESC);
