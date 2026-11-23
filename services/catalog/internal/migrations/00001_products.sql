-- products (catalog's own table). Same statements as db/schema/catalog.sql (the factory's input); CI checks the
-- two stay equal (scripts/migrations-check.sh). Append-only: once on main, never edit this file — add a new one.
-- +goose Up
CREATE TABLE IF NOT EXISTS products (
    sku         TEXT        NOT NULL PRIMARY KEY CHECK (sku ~ '^[A-Z0-9-]{3,32}$'),
    name        TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    category    TEXT        NOT NULL CHECK (length(category) BETWEEN 1 AND 50),
    price       NUMERIC     NOT NULL CHECK (price >= 0),
    status      INTEGER     NOT NULL DEFAULT 1 CHECK (status IN (1, 2)),   -- 1 on sale, 2 discontinued
    version     INTEGER     NOT NULL DEFAULT 1 CHECK (version > 0),        -- optimistic locking of the writes (R3)
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS products_category_sku_idx ON products (category, sku);

-- +goose Down
DROP TABLE products;
