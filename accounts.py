from db import get_pool


async def ensure_user_accounts(user_id: str) -> None:
    """指定ユーザーのPAL口座・CHIP口座が存在しなければ作成する"""
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for currency in ("PAL", "CHIP"):
                await conn.execute(
                    """
                    INSERT INTO bank.accounts (account_type, owner_id, currency, balance)
                    VALUES ('USER', $1, $2, 0)
                    ON CONFLICT DO NOTHING;
                    """,
                    user_id,
                    currency,
                )


async def get_user_balances(user_id: str) -> dict:
    """指定ユーザーのPAL・CHIP残高を取得する。存在しなければ0を返す"""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT currency, balance
            FROM bank.accounts
            WHERE account_type = 'USER' AND owner_id = $1;
            """,
            user_id,
        )

    balances = {"PAL": 0, "CHIP": 0}
    for row in rows:
        balances[row["currency"]] = row["balance"]

    return balances
