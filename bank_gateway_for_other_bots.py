import os, asyncpg
_pool=None
async def init_bank_gateway():
    global _pool
    _pool=await asyncpg.create_pool(os.environ["DATABASE_URL"],min_size=1,max_size=5)

async def bank_move(source_bot, reference_id, operation, user_id, currency, amount):
    """SHOP/CASINO/VOICE共通。operation=CREDITまたはDEBIT。reference_idは各Botで一意。"""
    if _pool is None: raise RuntimeError("init_bank_gateway() first")
    source_bot,operation,currency=source_bot.upper(),operation.upper(),currency.upper()
    if source_bot not in ("PAL_SHOP","PAL_CASINO","PAL_VOICE"): raise ValueError("source_bot")
    if operation not in ("CREDIT","DEBIT") or amount<=0: raise ValueError("operation/amount")
    async with _pool.acquire() as c:
      async with c.transaction():
        maintenance=await c.fetchval("SELECT setting_value FROM bank.settings WHERE setting_key='maintenance_mode'")
        if maintenance=="1": return {"status":"MAINTENANCE"}
        old=await c.fetchval("SELECT status FROM bank.integration_events WHERE source_bot=$1 AND external_reference_id=$2 FOR UPDATE",source_bot,reference_id)
        if old:return {"status":"ALREADY_PROCESSED","event_status":old}
        await c.execute("INSERT INTO bank.integration_events(source_bot,external_reference_id,operation,user_id,currency,amount,status) VALUES($1,$2,$3,$4,$5,$6,'PENDING')",source_bot,reference_id,operation,str(user_id),currency,amount)
        await c.execute("INSERT INTO bank.accounts(account_type,owner_id,currency,balance) VALUES('USER',$1,$2,0) ON CONFLICT DO NOTHING",str(user_id),currency)
        aid=await c.fetchval("SELECT account_id FROM bank.accounts WHERE account_type='USER' AND owner_id=$1 AND currency=$2",str(user_id),currency)
        if operation=="DEBIT":
            ok=await c.fetchval("UPDATE bank.accounts SET balance=balance-$1,updated_at=now() WHERE account_id=$2 AND balance >= $1 RETURNING account_id",amount,aid)
            if not ok:
                await c.execute("UPDATE bank.integration_events SET status='FAILED' WHERE source_bot=$1 AND external_reference_id=$2",source_bot,reference_id)
                return {"status":"INSUFFICIENT_BALANCE"}
            fr,to=aid,None
        else:
            await c.execute("UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",amount,aid);fr,to=None,aid
        await c.execute("""INSERT INTO bank.transactions(idempotency_key,transaction_type,currency,from_account_id,to_account_id,amount,external_bot,external_reference_id,status,completed_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'COMPLETED',now())""",f"INT:{source_bot}:{reference_id}",f"{source_bot}_{operation}",currency,fr,to,amount,source_bot,reference_id)
        await c.execute("UPDATE bank.integration_events SET status='COMPLETED',completed_at=now() WHERE source_bot=$1 AND external_reference_id=$2",source_bot,reference_id)
    return {"status":"SUCCESS"}

async def bank_credit(source_bot,reference_id,user_id,currency,amount):
    return await bank_move(source_bot,reference_id,"CREDIT",user_id,currency,amount)
async def bank_debit(source_bot,reference_id,user_id,currency,amount):
    return await bank_move(source_bot,reference_id,"DEBIT",user_id,currency,amount)
