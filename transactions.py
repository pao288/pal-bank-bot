import json

from db import get_pool


class InsufficientBalanceError(Exception):
    pass


class AlreadyProcessedError(Exception):
    pass


async def transfer(
    idempotency_key: str,
    transaction_type: str,
    currency: str,
    from_account_id: int | None,
    to_account_id: int | None,
    amount: int,
    external_bot: str | None = None,
    external_reference_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    if amount <= 0:
        raise ValueError("amount must be positive")

    currency = currency.upper()
    if currency not in ("PAL", "CHIP"):
        raise ValueError("invalid currency")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT status
                FROM bank.transactions
                WHERE idempotency_key=$1
                FOR UPDATE
                """,
                idempotency_key,
            )
            if existing is not None and existing["status"] == "COMPLETED":
                raise AlreadyProcessedError(idempotency_key)

            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO bank.transactions (
                        idempotency_key, transaction_type, currency,
                        from_account_id, to_account_id, amount,
                        external_bot, external_reference_id, status, metadata
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PENDING',$9::jsonb)
                    """,
                    idempotency_key,
                    transaction_type,
                    currency,
                    from_account_id,
                    to_account_id,
                    amount,
                    external_bot,
                    external_reference_id,
                    json.dumps(metadata or {}),
                )

            if from_account_id is not None:
                changed = await conn.fetchval(
                    """
                    UPDATE bank.accounts
                    SET balance=balance-$1, updated_at=now()
                    WHERE account_id=$2 AND balance >= $1
                    RETURNING account_id
                    """,
                    amount,
                    from_account_id,
                )
                if changed is None:
                    raise InsufficientBalanceError(idempotency_key)

            if to_account_id is not None:
                changed = await conn.fetchval(
                    """
                    UPDATE bank.accounts
                    SET balance=balance+$1, updated_at=now()
                    WHERE account_id=$2
                    RETURNING account_id
                    """,
                    amount,
                    to_account_id,
                )
                if changed is None:
                    raise RuntimeError("destination account not found")

            await conn.execute(
                """
                UPDATE bank.transactions
                SET status='COMPLETED', completed_at=now()
                WHERE idempotency_key=$1
                """,
                idempotency_key,
            )

    return {"status": "SUCCESS", "idempotency_key": idempotency_key}


async def get_history(user_id: str, limit: int = 100) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT t.bank_transaction_id, t.transaction_type, t.currency,
                   t.amount, t.status, t.created_at,
                   fa.owner_id AS from_owner_id,
                   ta.owner_id AS to_owner_id
            FROM bank.transactions t
            LEFT JOIN bank.accounts fa ON fa.account_id=t.from_account_id
            LEFT JOIN bank.accounts ta ON ta.account_id=t.to_account_id
            WHERE (fa.owner_id=$1 OR ta.owner_id=$1)
              AND COALESCE((t.metadata->>'hidden')::boolean, false)=false
            ORDER BY t.bank_transaction_id DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )


async def get_all_history(limit: int = 100) -> list:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT t.bank_transaction_id, t.transaction_type, t.currency,
                   t.amount, t.status, t.created_at,
                   fa.owner_id AS from_owner_id,
                   ta.owner_id AS to_owner_id
            FROM bank.transactions t
            LEFT JOIN bank.accounts fa ON fa.account_id=t.from_account_id
            LEFT JOIN bank.accounts ta ON ta.account_id=t.to_account_id
            ORDER BY t.bank_transaction_id DESC
            LIMIT $1
            """,
            limit,
        )
