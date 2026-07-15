import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_db_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await _create_schema(conn)
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized")
    return _pool


async def _create_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("""
    CREATE SCHEMA IF NOT EXISTS bank;

    CREATE TABLE IF NOT EXISTS bank.accounts (
        account_id BIGSERIAL PRIMARY KEY,
        account_type TEXT NOT NULL CHECK (account_type IN ('USER', 'SYSTEM')),
        owner_id TEXT,
        account_key TEXT,
        currency TEXT NOT NULL CHECK (currency IN ('PAL', 'CHIP')),
        balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_currency
        ON bank.accounts (owner_id, currency)
        WHERE account_type = 'USER';

    CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_system_key_currency
        ON bank.accounts (account_key, currency)
        WHERE account_type = 'SYSTEM';

    CREATE TABLE IF NOT EXISTS bank.transactions (
        bank_transaction_id BIGSERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        transaction_type TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency IN ('PAL', 'CHIP')),
        from_account_id BIGINT REFERENCES bank.accounts(account_id),
        to_account_id BIGINT REFERENCES bank.accounts(account_id),
        amount BIGINT NOT NULL CHECK (amount >= 0),
        external_bot TEXT,
        external_reference_id TEXT,
        status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED', 'REFUNDED')),
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        failed_at TIMESTAMPTZ
    );

    CREATE INDEX IF NOT EXISTS idx_transactions_external_ref
        ON bank.transactions (external_reference_id);

    CREATE TABLE IF NOT EXISTS bank.escrows (
        escrow_id BIGSERIAL PRIMARY KEY,
        external_type TEXT NOT NULL,
        external_reference_id TEXT NOT NULL UNIQUE,
        currency TEXT NOT NULL CHECK (currency IN ('PAL', 'CHIP')),
        amount BIGINT NOT NULL CHECK (amount >= 0),
        source_account_id BIGINT REFERENCES bank.accounts(account_id),
        destination_account_id BIGINT REFERENCES bank.accounts(account_id),
        status TEXT NOT NULL CHECK (status IN ('HELD', 'RELEASED', 'REFUNDED')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        released_at TIMESTAMPTZ,
        refunded_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS bank.pal_envelopes (
        envelope_id BIGSERIAL PRIMARY KEY,
        creator_id TEXT NOT NULL,
        total_amount BIGINT NOT NULL CHECK (total_amount > 0),
        max_claims INT NOT NULL CHECK (max_claims > 0),
        claimed_count INT NOT NULL DEFAULT 0,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'COMPLETED', 'CANCELLED')),
        source_channel_id TEXT,
        source_message_id TEXT,
        notice_channel_id TEXT,
        notice_message_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS bank.pal_envelope_slots (
        slot_id BIGSERIAL PRIMARY KEY,
        envelope_id BIGINT NOT NULL REFERENCES bank.pal_envelopes(envelope_id) ON DELETE CASCADE,
        amount BIGINT NOT NULL CHECK (amount > 0),
        slot_order INT NOT NULL,
        claimed_by TEXT,
        claimed_at TIMESTAMPTZ
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_envelope_slot_order
        ON bank.pal_envelope_slots(envelope_id, slot_order);

    CREATE TABLE IF NOT EXISTS bank.pal_envelope_claims (
        envelope_id BIGINT NOT NULL REFERENCES bank.pal_envelopes(envelope_id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        slot_id BIGINT NOT NULL REFERENCES bank.pal_envelope_slots(slot_id),
        amount BIGINT NOT NULL,
        claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (envelope_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS bank.settings (
        setting_key TEXT PRIMARY KEY,
        setting_value TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    INSERT INTO bank.settings (setting_key, setting_value)
    VALUES ('pal_envelope_max_claims', '100')
    ON CONFLICT (setting_key) DO NOTHING;

    INSERT INTO bank.accounts (account_type, account_key, currency, balance)
    VALUES
        ('SYSTEM', 'OFFICIAL_SHOP', 'PAL', 0),
        ('SYSTEM', 'VOICE_ROOM', 'PAL', 0)
    ON CONFLICT DO NOTHING;
    """)


    await conn.execute("""
    CREATE TABLE IF NOT EXISTS bank.transfer_requests (
        request_id BIGSERIAL PRIMARY KEY,
        requester_id TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        amount BIGINT NOT NULL CHECK (amount > 0),
        status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','REJECTED','FAILED')),
        reviewed_by TEXT,
        review_channel_id TEXT,
        review_message_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reviewed_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS bank.integration_events (
        event_id BIGSERIAL PRIMARY KEY,
        source_bot TEXT NOT NULL,
        external_reference_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        user_id TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency IN ('PAL','CHIP')),
        amount BIGINT NOT NULL CHECK (amount > 0),
        status TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        UNIQUE(source_bot, external_reference_id)
    );

    INSERT INTO bank.settings(setting_key, setting_value) VALUES
      ('chip_rate_pal','100'),
      ('exchange_fee_percent','0'),
      ('exchange_min_pal','1000')
    ON CONFLICT(setting_key) DO NOTHING;
    """)


    await conn.execute("""
    ALTER TABLE bank.transfer_requests
        ADD COLUMN IF NOT EXISTS hold_transaction_id BIGINT,
        ADD COLUMN IF NOT EXISTS warning_text TEXT;

    CREATE TABLE IF NOT EXISTS bank.notifications (
        notification_id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        notification_type TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        is_read BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_bank_notifications_user
        ON bank.notifications(user_id, is_read, created_at DESC);

    CREATE TABLE IF NOT EXISTS bank.transaction_reversals (
        reversal_id BIGSERIAL PRIMARY KEY,
        original_transaction_id BIGINT NOT NULL REFERENCES bank.transactions(bank_transaction_id),
        reversal_transaction_id BIGINT NOT NULL REFERENCES bank.transactions(bank_transaction_id),
        reversed_by TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(original_transaction_id)
    );

    CREATE TABLE IF NOT EXISTS bank.daily_stats (
        stat_date DATE PRIMARY KEY,
        completed_transactions BIGINT NOT NULL DEFAULT 0,
        moved_pal BIGINT NOT NULL DEFAULT 0,
        moved_chip BIGINT NOT NULL DEFAULT 0,
        transfer_requests BIGINT NOT NULL DEFAULT 0,
        approved_transfers BIGINT NOT NULL DEFAULT 0,
        rejected_transfers BIGINT NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    INSERT INTO bank.settings(setting_key, setting_value) VALUES
        ('maintenance_mode','0'),
        ('movement_log_channel_id','0'),
        ('bank_status_channel_id','0'),
        ('bank_status_message_id','0'),
        ('large_transfer_warning_pal','100000'),
        ('rapid_transfer_warning_count','5'),
        ('rapid_transfer_warning_minutes','10')
    ON CONFLICT(setting_key) DO NOTHING;
    """)


async def get_setting(key: str, default: str | None = None) -> str | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT setting_value FROM bank.settings WHERE setting_key=$1",
            key,
        )
    return value if value is not None else default


async def set_setting(key: str, value: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bank.settings(setting_key, setting_value, updated_at)
            VALUES ($1,$2,now())
            ON CONFLICT(setting_key)
            DO UPDATE SET setting_value=EXCLUDED.setting_value, updated_at=now()
            """,
            key,
            value,
        )
