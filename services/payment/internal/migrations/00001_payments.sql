-- payments (payment's own tables). Same statements as db/schema/payment.sql (the factory's input); CI checks
-- the two stay equal (scripts/migrations-check.sh). Append-only: once on main, never edit this file — add a new one.
-- +goose Up
-- storefront v2 · payment (支付, simulated). This service's own database.
-- One payment per order: the order id is the idempotency key of a charge (a repeated charge returns the first).
CREATE TABLE IF NOT EXISTS payments (
    order_id    BIGINT      NOT NULL PRIMARY KEY CHECK (order_id > 0),
    amount      NUMERIC     NOT NULL CHECK (amount >= 0),
    status      INTEGER     NOT NULL CHECK (status IN (1, 2)),   -- 1 succeeded, 2 failed
    reason      TEXT,                                            -- why it failed (null when it succeeded)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE payments;
