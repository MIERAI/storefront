-- Data migration (strangler step S1): the products v1 kept in code (services/api, `var catalog`) move into this table.
-- v1 id → SKU: 1 → BEAN-001, 2 → BAG-002, 3 → MUG-003; prices in yen, unchanged. Categories are new in v2 (ListProducts
-- lists one category). ON CONFLICT DO NOTHING: a product an operator already created or changed is kept as it is.
-- +goose Up
INSERT INTO products (sku, name, category, price) VALUES
    ('BEAN-001', 'coffee beans', 'coffee', 1200),
    ('BAG-002',  'drip bag',     'coffee', 180),
    ('MUG-003',  'mug',          'goods',  1600)
ON CONFLICT (sku) DO NOTHING;

-- +goose Down
-- Only the rows this migration wrote and nobody changed since (version 1).
DELETE FROM products WHERE sku IN ('BEAN-001', 'BAG-002', 'MUG-003') AND version = 1;
