-- storefront v2 · catalog (商品). This service's own database: no other service reads or writes it.
-- Written by the factory first (readiness R1); storefront v2 implements against it.
CREATE TABLE IF NOT EXISTS products (
    sku         TEXT        NOT NULL PRIMARY KEY CHECK (sku ~ '^[A-Z0-9-]{3,32}$'),
    name        TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    category    TEXT        NOT NULL CHECK (length(category) BETWEEN 1 AND 50),
    price       NUMERIC     NOT NULL CHECK (price >= 0),
    status      INTEGER     NOT NULL DEFAULT 1 CHECK (status IN (1, 2)),   -- 1 on sale, 2 discontinued
    version     INTEGER     NOT NULL DEFAULT 1 CHECK (version > 0),        -- optimistic locking of the writes (R3)
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ListProducts: by category, keyset on sku
CREATE INDEX IF NOT EXISTS products_category_sku_idx ON products (category, sku);
