import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_db_pool() -> asyncpg.Pool:
    """DB接続プールを作成し、必要なschema・table・初期データを作成する"""
    global _pool

    database_url = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await _create_schema(conn)

    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized yet")
    return _pool


async def _create_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE SCHEMA IF NOT EXISTS bank;")

    await conn.execute("""
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
    """)

    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_currency
            ON bank.accounts (owner_id, currency)
            WHERE account_type = 'USER';
    """)

    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_system_key_currency
            ON bank.accounts (account_key, currency)
            WHERE account_type = 'SYSTEM';
    """)

    await conn.execute("""
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
            metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ
        );
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_external_ref
            ON bank.transactions (external_reference_id);
    """)

    await conn.execute("""
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
    """)

    await conn.execute("""
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
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank.pal_envelope_slots (
            slot_id BIGSERIAL PRIMARY KEY,
            envelope_id BIGINT NOT NULL REFERENCES bank.pal_envelopes(envelope_id),
            amount BIGINT NOT NULL CHECK (amount > 0),
            slot_order INT NOT NULL,
            claimed_by TEXT,
            claimed_at TIMESTAMPTZ
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank.pal_envelope_claims (
            envelope_id BIGINT NOT NULL REFERENCES bank.pal_envelopes(envelope_id),
            user_id TEXT NOT NULL,
            slot_id BIGINT NOT NULL REFERENCES bank.pal_envelope_slots(slot_id),
            amount BIGINT NOT NULL,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (envelope_id, user_id)
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bank.settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    await conn.execute("""
        INSERT INTO bank.settings (setting_key, setting_value)
        VALUES ('pal_envelope_max_claims', '100')
        ON CONFLICT (setting_key) DO NOTHING;
    """)

    await conn.execute("""
        INSERT INTO bank.accounts (account_type, account_key, currency, balance)
        VALUES ('SYSTEM', 'OFFICIAL_SHOP', 'PAL', 0)
        ON CONFLICT DO NOTHING;
    """)

    await conn.execute("""
        INSERT INTO bank.accounts (account_type, account_key, currency, balance)
        VALUES ('SYSTEM', 'VOICE_ROOM', 'PAL', 0)
        ON CONFLICT DO NOTHING;
    """)
