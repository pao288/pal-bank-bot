import uuid
from accounts import get_user_account_id, unscope
from db import get_pool, get_setting
from transactions import transfer

async def exchange(user_id: str, direction: str, amount: int, guild_id=None):
    rate = int(await get_setting("chip_rate_pal", "100", guild_id=guild_id))
    fee_pct = int(await get_setting("exchange_fee_percent", "0", guild_id=guild_id))
    minimum = int(await get_setting("exchange_min_pal", "1000", guild_id=guild_id))
    if amount <= 0: raise ValueError("1以上で入力してください")
    if direction == "P2C":
        if amount < minimum: raise ValueError(f"最低交換額は{minimum:,} PALです")
        fee = amount * fee_pct // 100
        chips = (amount - fee) // rate
        if chips <= 0: raise ValueError("交換後のCHIPが0です")
        pal = await get_user_account_id(user_id, "PAL")
        chip = await get_user_account_id(user_id, "CHIP")
        await transfer(f"EX:P2C:D:{uuid.uuid4()}","EXCHANGE_P2C","PAL",pal,None,amount,external_bot="PAL_BANK",metadata={"hidden":True})
        await transfer(f"EX:P2C:C:{uuid.uuid4()}","EXCHANGE_P2C","CHIP",None,chip,chips,external_bot="PAL_BANK",metadata={"hidden":True})
        return amount, "PAL", chips, "CHIP"
    fee = amount * rate * fee_pct // 100
    pals = amount * rate - fee
    chip = await get_user_account_id(user_id, "CHIP")
    pal = await get_user_account_id(user_id, "PAL")
    await transfer(f"EX:C2P:D:{uuid.uuid4()}","EXCHANGE_C2P","CHIP",chip,None,amount,external_bot="PAL_BANK",metadata={"hidden":True})
    await transfer(f"EX:C2P:C:{uuid.uuid4()}","EXCHANGE_C2P","PAL",None,pal,pals,external_bot="PAL_BANK",metadata={"hidden":True})
    return amount, "CHIP", pals, "PAL"

async def totals(guild_id=None):
    pool=get_pool()
    prefix = f"{guild_id}:%" if guild_id is not None else "%"
    async with pool.acquire() as c:
        rows=await c.fetch(
            "SELECT currency,COALESCE(SUM(balance),0)::bigint total FROM bank.accounts WHERE owner_id LIKE $1 GROUP BY currency",
            prefix,
        )
    d={"PAL":0,"CHIP":0}
    for r in rows:d[r["currency"]]=r["total"]
    return d

async def rankings(limit=10, guild_id=None):
    rate=int(await get_setting("chip_rate_pal","100", guild_id=guild_id))
    pool=get_pool()
    prefix = f"{guild_id}:%" if guild_id is not None else "%"
    async with pool.acquire() as c:
        pal=await c.fetch("SELECT owner_id,balance FROM bank.accounts WHERE account_type='USER' AND currency='PAL' AND balance>0 AND owner_id LIKE $2 ORDER BY balance DESC LIMIT $1",limit,prefix)
        chip=await c.fetch("SELECT owner_id,balance FROM bank.accounts WHERE account_type='USER' AND currency='CHIP' AND balance>0 AND owner_id LIKE $2 ORDER BY balance DESC LIMIT $1",limit,prefix)
        total=await c.fetch("""SELECT owner_id,SUM(CASE WHEN currency='PAL' THEN balance ELSE balance*$1 END)::bigint balance FROM bank.accounts WHERE account_type='USER' AND owner_id LIKE $3 GROUP BY owner_id HAVING SUM(CASE WHEN currency='PAL' THEN balance ELSE balance*$1 END)>0 ORDER BY balance DESC LIMIT $2""",rate,limit,prefix)
    # 表示用にguild_idプレフィックスを取り除いた素のuser_idへ変換したレコードを返す
    def _strip(rows):
        return [{**dict(r), "owner_id": unscope(r["owner_id"])} for r in rows]
    return _strip(pal), _strip(chip), _strip(total)
