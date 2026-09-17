CREATE TABLE events (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    sport       text NOT NULL,
    home_id     text NOT NULL,
    away_id     text NOT NULL,
    home        text[] NOT NULL,
    away        text[] NOT NULL,
    starts_at   timestamptz,
    indexed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tournaments (
    id         text PRIMARY KEY,
    upcoming   integer NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now()
);
