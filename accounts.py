from db import get_pool


def scope(guild_id, raw_id) -> str:
    """サーバー間で残高が混ざらないよう、guild_idを埋め込んだキーを作る。
    例: scope(123456789, 987654321) -> "123456789:987654321" """
    return f"{guild_id}:{raw_id}"


def unscope(scoped_id: str) -> str:
    """scope()で作ったキーから元のuser_id部分だけを取り出す（表示用）。
    まだ移行(!bankmigrate)されていない旧形式のキーはそのまま返す。"""
    if scoped_id and ":" in scoped_id:
        return scoped_id.split(":", 1)[1]
    return scoped_id


# 以降の関数はすべて「guild_idで既にスコープ済みのuser_idキー」を受け取る前提。
# （呼び出し側で scope(guild_id, user.id) してから渡すこと）

async def ensure_user_accounts(user_id: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for currency in ("PAL", "CHIP"):
                await conn.execute(
                    """
                    INSERT INTO bank.accounts (account_type, owner_id, currency, balance)
                    VALUES ('USER', $1, $2, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    user_id,
                    currency,
                )


async def get_user_balances(user_id: str) -> dict[str, int]:
    await ensure_user_accounts(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT currency, balance
            FROM bank.accounts
            WHERE account_type='USER' AND owner_id=$1
            """,
            user_id,
        )
    result = {"PAL": 0, "CHIP": 0}
    for row in rows:
        result[row["currency"]] = row["balance"]
    return result


async def get_user_account_id(user_id: str, currency: str) -> int:
    currency = currency.upper()
    await ensure_user_accounts(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        account_id = await conn.fetchval(
            """
            SELECT account_id
            FROM bank.accounts
            WHERE account_type='USER' AND owner_id=$1 AND currency=$2
            """,
            user_id,
            currency,
        )
    if account_id is None:
        raise RuntimeError("Account not found")
    return account_id
