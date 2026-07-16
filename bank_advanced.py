import csv
import io
import uuid
from datetime import datetime, timezone

from accounts import ensure_user_accounts, unscope
from db import get_pool, get_setting
from transactions import InsufficientBalanceError


async def maintenance_enabled(guild_id=None) -> bool:
    return await get_setting("maintenance_mode", "0", guild_id=guild_id) == "1"


async def set_maintenance(enabled: bool, guild_id=None) -> None:
    from db import set_setting
    await set_setting("maintenance_mode", "1" if enabled else "0", guild_id=guild_id)


async def add_notification(user_id: str, kind: str, title: str, body: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO bank.notifications(user_id,notification_type,title,body)
               VALUES($1,$2,$3,$4)""",
            user_id, kind, title, body,
        )


async def get_notifications(user_id: str, limit: int = 10):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT notification_id,title,body,is_read,created_at
               FROM bank.notifications WHERE user_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            user_id, limit,
        )
        await conn.execute(
            "UPDATE bank.notifications SET is_read=TRUE WHERE user_id=$1 AND is_read=FALSE",
            user_id,
        )
    return rows


async def unread_count(user_id: str) -> int:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM bank.notifications WHERE user_id=$1 AND is_read=FALSE",
            user_id,
        )


async def profile(user_id: str, guild_id=None) -> dict:
    await ensure_user_accounts(user_id)
    rate = int(await get_setting("chip_rate_pal", "100", guild_id=guild_id))
    pool = get_pool()
    prefix = f"{guild_id}:%" if guild_id is not None else "%"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT currency,balance,created_at FROM bank.accounts WHERE account_type='USER' AND owner_id=$1",
            user_id,
        )
        pending = await conn.fetchval(
            """SELECT COALESCE(SUM(amount),0)::bigint FROM bank.transfer_requests
               WHERE requester_id=$1 AND status='PENDING'""",
            unscope(user_id),
        )
        rank = await conn.fetchval(
            """WITH assets AS (
                 SELECT owner_id,
                 SUM(CASE WHEN currency='PAL' THEN balance ELSE balance*$1 END)::bigint asset
                 FROM bank.accounts WHERE account_type='USER' AND owner_id LIKE $3
                 GROUP BY owner_id
               ), ranked AS (
                 SELECT owner_id,DENSE_RANK() OVER(ORDER BY asset DESC) AS rank FROM assets
               )
               SELECT rank FROM ranked WHERE owner_id=$2""",
            rate, user_id, prefix,
        )
    balances = {"PAL": 0, "CHIP": 0}
    created = None
    for row in rows:
        balances[row["currency"]] = row["balance"]
        created = row["created_at"] if created is None or row["created_at"] < created else created
    return {
        **balances,
        "pending_pal": pending,
        "available_pal": max(0, balances["PAL"] - pending),
        "asset_pal": balances["PAL"] + balances["CHIP"] * rate,
        "rank": rank or 0,
        "created_at": created,
        "rate": rate,
        "unread": await unread_count(unscope(user_id)),
    }


async def transfer_warning(requester_id: str, amount: int) -> str | None:
    threshold = int(await get_setting("large_transfer_warning_pal", "100000"))
    count_limit = int(await get_setting("rapid_transfer_warning_count", "5"))
    minutes = int(await get_setting("rapid_transfer_warning_minutes", "10"))
    warnings = []
    if amount >= threshold:
        warnings.append(f"高額送金: {amount:,} PAL")
    pool = get_pool()
    async with pool.acquire() as conn:
        recent = await conn.fetchval(
            """SELECT COUNT(*) FROM bank.transfer_requests
               WHERE requester_id=$1
                 AND created_at >= now() - ($2::text || ' minutes')::interval""",
            requester_id, str(minutes),
        )
    if recent >= count_limit:
        warnings.append(f"{minutes}分以内の申請数: {recent + 1}件")
    return " / ".join(warnings) if warnings else None


async def duplicate_pending(requester_id: str, recipient_id: str, amount: int):
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """SELECT request_id FROM bank.transfer_requests
               WHERE requester_id=$1 AND recipient_id=$2 AND amount=$3
                 AND status='PENDING'
                 AND created_at >= now() - interval '2 minutes'
               ORDER BY created_at DESC LIMIT 1""",
            requester_id, recipient_id, amount,
        )


async def atomic_exchange(user_id: str, direction: str, amount: int, guild_id=None) -> tuple[int, str, int, str, str]:
    if await maintenance_enabled(guild_id=guild_id):
        raise ValueError("BANKはメンテナンスモード中です。")
    await ensure_user_accounts(user_id)
    if amount <= 0:
        raise ValueError("1以上で入力してください。")
    rate = int(await get_setting("chip_rate_pal", "100", guild_id=guild_id))
    fee_pct = int(await get_setting("exchange_fee_percent", "0", guild_id=guild_id))
    minimum = int(await get_setting("exchange_min_pal", "1000", guild_id=guild_id))
    pool = get_pool()
    tx_code = f"PAL-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"

    async with pool.acquire() as conn:
        async with conn.transaction():
            pal_id = await conn.fetchval(
                "SELECT account_id FROM bank.accounts WHERE account_type='USER' AND owner_id=$1 AND currency='PAL'",
                user_id,
            )
            chip_id = await conn.fetchval(
                "SELECT account_id FROM bank.accounts WHERE account_type='USER' AND owner_id=$1 AND currency='CHIP'",
                user_id,
            )
            if direction == "P2C":
                if amount < minimum:
                    raise ValueError(f"最低交換額は{minimum:,} PALです。")
                fee = amount * fee_pct // 100
                received = (amount - fee) // rate
                if received <= 0:
                    raise ValueError("交換後のCHIPが0です。")
                ok = await conn.fetchval(
                    """UPDATE bank.accounts SET balance=balance-$1,updated_at=now()
                       WHERE account_id=$2 AND balance >= $1 RETURNING account_id""",
                    amount, pal_id,
                )
                if ok is None:
                    raise InsufficientBalanceError(tx_code)
                await conn.execute(
                    "UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",
                    received, chip_id,
                )
                source_amount, source_currency, target_amount, target_currency = amount, "PAL", received, "CHIP"
            else:
                gross = amount * rate
                fee = gross * fee_pct // 100
                received = gross - fee
                ok = await conn.fetchval(
                    """UPDATE bank.accounts SET balance=balance-$1,updated_at=now()
                       WHERE account_id=$2 AND balance >= $1 RETURNING account_id""",
                    amount, chip_id,
                )
                if ok is None:
                    raise InsufficientBalanceError(tx_code)
                await conn.execute(
                    "UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",
                    received, pal_id,
                )
                source_amount, source_currency, target_amount, target_currency = amount, "CHIP", received, "PAL"

            await conn.execute(
                """INSERT INTO bank.transactions(
                    idempotency_key,transaction_type,currency,amount,external_bot,
                    external_reference_id,status,metadata,completed_at
                   ) VALUES($1,'CURRENCY_EXCHANGE',$2,$3,'PAL_BANK',$4,'COMPLETED',
                    jsonb_build_object(
                      'hidden',true,'public_tx_id',$4::text,'user_id',$5::text,
                      'source_amount',$6::bigint,'source_currency',$7::text,
                      'target_amount',$8::bigint,'target_currency',$9::text,
                      'fee',$10::bigint
                    ),now())""",
                f"EXCHANGE:{tx_code}", source_currency, source_amount, tx_code, user_id,
                source_amount, source_currency, target_amount, target_currency, fee,
            )
    return source_amount, source_currency, target_amount, target_currency, tx_code


async def statistics() -> dict:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT
                COUNT(*) FILTER (WHERE status='COMPLETED')::bigint completed,
                COALESCE(SUM(amount) FILTER (WHERE status='COMPLETED' AND currency='PAL'),0)::bigint moved_pal,
                COALESCE(SUM(amount) FILTER (WHERE status='COMPLETED' AND currency='CHIP'),0)::bigint moved_chip,
                COUNT(*) FILTER (WHERE status='COMPLETED' AND completed_at >= now()-interval '24 hours')::bigint tx24
               FROM bank.transactions"""
        )
        req = await conn.fetchrow(
            """SELECT COUNT(*)::bigint total,
               COUNT(*) FILTER(WHERE status='PENDING')::bigint pending,
               COUNT(*) FILTER(WHERE status='APPROVED')::bigint approved,
               COUNT(*) FILTER(WHERE status='REJECTED')::bigint rejected
               FROM bank.transfer_requests"""
        )
        users = await conn.fetchval(
            "SELECT COUNT(DISTINCT owner_id) FROM bank.accounts WHERE account_type='USER'"
        )
    return {**dict(row), **{f"req_{k}": v for k, v in dict(req).items()}, "users": users}


async def search_transactions(user_id: str | None = None, currency: str | None = None,
                              source: str | None = None, limit: int = 25):
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT t.*,fa.owner_id from_user,ta.owner_id to_user
               FROM bank.transactions t
               LEFT JOIN bank.accounts fa ON fa.account_id=t.from_account_id
               LEFT JOIN bank.accounts ta ON ta.account_id=t.to_account_id
               WHERE ($1::text IS NULL OR fa.owner_id=$1 OR ta.owner_id=$1 OR t.metadata->>'user_id'=$1)
                 AND ($2::text IS NULL OR t.currency=$2)
                 AND ($3::text IS NULL OR t.external_bot=$3 OR t.transaction_type ILIKE '%'||$3||'%')
               ORDER BY t.created_at DESC LIMIT $4""",
            user_id, currency, source, limit,
        )


async def reverse_transaction(transaction_id: int, admin_id: str, reason: str) -> str:
    pool = get_pool()
    code = f"PAL-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            original = await conn.fetchrow(
                "SELECT * FROM bank.transactions WHERE bank_transaction_id=$1 AND status='COMPLETED' FOR UPDATE",
                transaction_id,
            )
            if original is None:
                raise ValueError("対象取引が見つからないか、完了取引ではありません。")
            exists = await conn.fetchval(
                "SELECT 1 FROM bank.transaction_reversals WHERE original_transaction_id=$1",
                transaction_id,
            )
            if exists:
                raise ValueError("この取引は取消済みです。")
            fr, to = original["from_account_id"], original["to_account_id"]
            amount = original["amount"]
            if to is not None:
                ok = await conn.fetchval(
                    """UPDATE bank.accounts SET balance=balance-$1,updated_at=now()
                       WHERE account_id=$2 AND balance >= $1 RETURNING account_id""",
                    amount, to,
                )
                if ok is None:
                    raise ValueError("取消元の受取口座残高が不足しています。")
            if fr is not None:
                await conn.execute(
                    "UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",
                    amount, fr,
                )
            reversal_id = await conn.fetchval(
                """INSERT INTO bank.transactions(
                    idempotency_key,transaction_type,currency,from_account_id,to_account_id,
                    amount,external_bot,external_reference_id,status,metadata,completed_at
                   ) VALUES($1,'ADMIN_REVERSAL',$2,$3,$4,$5,'ADMIN',$6,'COMPLETED',
                     jsonb_build_object('public_tx_id',$6::text,'original_transaction_id',$7::bigint,
                     'reason',$8::text,'admin_id',$9::text),now())
                   RETURNING bank_transaction_id""",
                f"REVERSAL:{transaction_id}", original["currency"], to, fr, amount, code,
                transaction_id, reason, admin_id,
            )
            await conn.execute(
                """INSERT INTO bank.transaction_reversals(
                    original_transaction_id,reversal_transaction_id,reversed_by,reason
                   ) VALUES($1,$2,$3,$4)""",
                transaction_id, reversal_id, admin_id, reason,
            )
    return code


async def claim_legacy_data(guild_id) -> dict:
    """サーバー分離（guild_idスコープ）を導入する前の残高・設定を、このサーバーの物として引き継ぐ。
    1度だけ実行することを想定（既にスコープ済み＝":"を含むキーは対象外なので、複数回実行しても安全）。"""
    pool = get_pool()
    prefix = f"{guild_id}:"
    async with pool.acquire() as conn:
        async with conn.transaction():
            accounts = await conn.execute(
                """UPDATE bank.accounts SET owner_id=$1||owner_id
                   WHERE account_type='USER' AND owner_id NOT LIKE '%:%'""",
                prefix,
            )
            system_accounts = await conn.execute(
                """UPDATE bank.accounts SET account_key=$1||account_key
                   WHERE account_type='SYSTEM' AND account_key NOT LIKE '%:%'""",
                prefix,
            )
            settings = await conn.execute(
                """UPDATE bank.settings SET setting_key=$1||setting_key
                   WHERE setting_key NOT LIKE '%:%'""",
                prefix,
            )
    def _count(tag: str) -> int:
        try: return int(tag.split()[-1])
        except (ValueError, IndexError): return 0
    return {
        "accounts": _count(accounts),
        "system_accounts": _count(system_accounts),
        "settings": _count(settings),
    }


async def csv_export_bytes() -> bytes:
    rows = await search_transactions(limit=5000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["db_id","transaction_type","currency","amount","from_user","to_user",
                     "external_bot","status","created_at","public_tx_id","reason"])
    for r in rows:
        meta = r["metadata"] or {}
        writer.writerow([
            r["bank_transaction_id"], r["transaction_type"], r["currency"], r["amount"],
            r["from_user"], r["to_user"], r["external_bot"], r["status"],
            r["created_at"].isoformat(), meta.get("public_tx_id",""), meta.get("reason",""),
        ])
    return output.getvalue().encode("utf-8-sig")


async def confiscate_user(scoped_user_id: str, admin_id: str, currency: str | None = None) -> dict:
    """指定したユーザー（このサーバー内）の残高を全没収する。currency未指定ならPAL/CHIP両方。
    金額は消滅するのではなく、監査用に取引履歴として記録される（from側のみ、to側なし）。"""
    pool = get_pool()
    currencies = [currency.upper()] if currency else ["PAL", "CHIP"]
    seized = {"PAL": 0, "CHIP": 0}
    code = f"PAL-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    async with pool.acquire() as conn:
        async with conn.transaction():
            for cur in currencies:
                acc = await conn.fetchrow(
                    """SELECT account_id,balance FROM bank.accounts
                       WHERE account_type='USER' AND owner_id=$1 AND currency=$2 FOR UPDATE""",
                    scoped_user_id, cur,
                )
                if not acc or acc["balance"] <= 0:
                    continue
                amount = acc["balance"]
                await conn.execute(
                    "UPDATE bank.accounts SET balance=0,updated_at=now() WHERE account_id=$1",
                    acc["account_id"],
                )
                await conn.execute(
                    """INSERT INTO bank.transactions(
                        idempotency_key,transaction_type,currency,from_account_id,to_account_id,
                        amount,external_bot,external_reference_id,status,metadata,completed_at
                       ) VALUES($1,'ADMIN_CONFISCATE',$2,$3,NULL,$4,'ADMIN',$5,'COMPLETED',
                         jsonb_build_object('public_tx_id',$5::text,'admin_id',$6::text,'target_user',$7::text),now())""",
                    f"CONFISCATE:{code}:{cur}", cur, acc["account_id"], amount, code, admin_id, scoped_user_id,
                )
                seized[cur] = amount
    return seized


async def confiscate_all(guild_id, admin_id: str, currency: str | None = None) -> dict:
    """このサーバー内の全ユーザーの残高を一括没収する。currency未指定ならPAL/CHIP両方。
    ユーザーごとに監査用の取引履歴が残る（from側のみ、to側なし）。"""
    pool = get_pool()
    currencies = [currency.upper()] if currency else ["PAL", "CHIP"]
    prefix = f"{guild_id}:%"
    code = f"PAL-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    seized = {"PAL": 0, "CHIP": 0}
    affected_users = set()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for cur in currencies:
                rows = await conn.fetch(
                    """SELECT account_id,owner_id,balance FROM bank.accounts
                       WHERE account_type='USER' AND currency=$1 AND owner_id LIKE $2 AND balance>0
                       FOR UPDATE""",
                    cur, prefix,
                )
                for acc in rows:
                    await conn.execute(
                        "UPDATE bank.accounts SET balance=0,updated_at=now() WHERE account_id=$1",
                        acc["account_id"],
                    )
                    await conn.execute(
                        """INSERT INTO bank.transactions(
                            idempotency_key,transaction_type,currency,from_account_id,to_account_id,
                            amount,external_bot,external_reference_id,status,metadata,completed_at
                           ) VALUES($1,'ADMIN_CONFISCATE_ALL',$2,$3,NULL,$4,'ADMIN',$5,'COMPLETED',
                             jsonb_build_object('public_tx_id',$5::text,'admin_id',$6::text,'target_user',$7::text),now())""",
                        f"CONFISCATE_ALL:{code}:{acc['account_id']}", cur, acc["account_id"], acc["balance"], code, admin_id, acc["owner_id"],
                    )
                    seized[cur] += acc["balance"]
                    affected_users.add(acc["owner_id"])
    return {"seized": seized, "code": code, "affected_accounts": len(affected_users)}
