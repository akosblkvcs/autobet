CREATE TABLE bookmakers (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug       text        NOT NULL UNIQUE,
    name       text        NOT NULL,
    config     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    enabled    boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO bookmakers (slug, name) VALUES ('tippmixpro', 'tippmixpro.hu');
