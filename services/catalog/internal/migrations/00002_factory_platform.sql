-- The factory's platform tables (outbox + consumer de-duplication): db/schema/zz_factory_platform.sql, which the
-- factory copies from api-factory-runtime (outbox.PlatformSQL). A new runtime version that changes them = a new
-- migration here (CI's drift check fails until there is one).
-- +goose Up
CREATE TABLE IF NOT EXISTS factory_outbox (
    seq          BIGSERIAL   PRIMARY KEY,
    id           UUID        NOT NULL UNIQUE,      -- envelope.id(UUIDv7;重发时不变)
    kind         SMALLINT    NOT NULL,             -- 1 event, 2 command
    type         TEXT        NOT NULL,
    version      INTEGER     NOT NULL,
    subject      TEXT        NOT NULL,
    envelope     JSONB       NOT NULL,             -- 完整的消息(发送时原样发出)
    occurred_at  TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,                      -- NULL = 还没发出(broker 确认过才填)
    attempts     INTEGER     NOT NULL DEFAULT 0,   -- 发送失败的次数
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS factory_outbox_pending_idx ON factory_outbox (seq) WHERE published_at IS NULL;
CREATE INDEX IF NOT EXISTS factory_outbox_published_idx ON factory_outbox (published_at) WHERE published_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS factory_processed_messages (  -- 消费端去重: (处理器, 消息 id) 处理过了
    handler      TEXT        NOT NULL,
    message_id   UUID        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (handler, message_id)
);
CREATE INDEX IF NOT EXISTS factory_processed_messages_at_idx ON factory_processed_messages (processed_at);

-- +goose Down
DROP TABLE factory_processed_messages;
DROP TABLE factory_outbox;
