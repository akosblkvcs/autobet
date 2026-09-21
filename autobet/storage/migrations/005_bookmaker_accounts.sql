CREATE TABLE bookmaker_accounts (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    bookmaker_id  bigint      NOT NULL REFERENCES bookmakers (id) ON DELETE CASCADE,
    username      text        NOT NULL,
    secret        bytea       NOT NULL,
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'disabled')),
    last_login_at timestamptz,
    last_error    text        NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, bookmaker_id)
);
