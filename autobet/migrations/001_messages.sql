CREATE TABLE messages (
    source      text        NOT NULL,
    external_id text        NOT NULL,
    channel     text        NOT NULL,
    sent_at     timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    text        text        NOT NULL,
    origin      text        NOT NULL DEFAULT 'live',
    PRIMARY KEY (source, external_id)
);

CREATE INDEX messages_received_at_idx ON messages (received_at DESC);
