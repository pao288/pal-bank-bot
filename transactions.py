from db import get_pool


class InsufficientBalanceError(Exception):
    pass


class AlreadyProcessedError(Exception):
    """同一idempotency_keyで既にCOMPLETED済みの場合"""
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
) -> dict:
    """
    口座間で通貨を移動させる中心処理。
    from_account_id が None の場合は「付与のみ」(残高チェックなし)。
    to_account_id が None の場合は「減算のみ」。
    同一 idempotency_key の COMPLETED 処理は再実行しない。
    """
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT status FROM bank.transactions
                WHERE idempotency_key = $1
                FOR UPDATE;
                """,
                idempotency_key,
            )

            if existing is not None:
                if existing["status"] == "COMPLETED":
                    raise AlreadyProcessedError(idempotency_key)

            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO bank.transactions (
                        idempotency_key, transaction_type, currency,
                        from_account_id, to_account_id, amount,
                        external_bot, external_reference_id, status
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'PENDING');
                    """,
                    idempotency_key, transaction_type, currency,
                    from_account_id, to_account_id, amount,
                    external_bot, external_reference_id,
                )

            if from_account_id is not None:
                account = await conn.fetchrow(
                    """
                    SELECT balance FROM bank.accounts
                    WHERE account_id = $1
                    FOR UPDATE;
                    """,
                    from_account_id,
                )
                if account is None or account["balance"] < amount:
                    await conn.execute(
                        """
                        UPDATE bank.transactions
                        SET status = 'FAILED', failed_at = now()
                        WHERE idempotency_key = $1;
                        """,
                        idempotency_key,
                    )
                    raise InsufficientBalanceError(idempotency_key)

                await conn.execute(
                    """
                    UPDATE bank.accounts
                    SET balance = balance - $1, updated_at = now()
                    WHERE account_id = $2;
                    """,
                    amount, from_account_id,
                )

            if to_account_id is not None:
                await conn.execute(
                    """
                    UPDATE bank.accounts
                    SET balance = balance + $1, updated_at = now()
                    WHERE account_id = $2;
                    """,
                    amount, to_account_id,
                )

            await conn.execute(
                """
                UPDATE bank.transactions
                SET status = 'COMPLETED', completed_at = now()
                WHERE idempotency_key = $1;
                """,
                idempotency_key,
            )

    return {"status": "SUCCESS", "idempotency_key": idempotency_key}


async def get_user_account_id(user_id: str, currency: str) -> int | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_id FROM bank.accounts
            WHERE account_type = 'USER' AND owner_id = $1 AND currency = $2;
            """,
            user_id, currency,
        )
    return row["account_id"] if row else None
