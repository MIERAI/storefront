-- storefront v2 · notification (通知). This service's own database.
-- event_id is unique: the same event delivered twice is recorded (and sent) once.
CREATE TABLE IF NOT EXISTS notifications (
    event_id    TEXT        NOT NULL PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 64),
    order_id    BIGINT      NOT NULL CHECK (order_id > 0),
    kind        INTEGER     NOT NULL CHECK (kind IN (1, 2)),   -- 1 order confirmed, 2 order cancelled
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS notifications_order_idx ON notifications (order_id, sent_at);
