import os
import logging
import uuid

import discord
from discord.ext import commands

from db import init_db_pool
from views import BankPanelView
from accounts import ensure_user_accounts
from transactions import transfer, get_user_account_id, InsufficientBalanceError, AlreadyProcessedError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pal_bank")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    bot.add_view(BankPanelView())
    logger.info(f"PAL BANK BOT起動完了: {bot.user}")


@bot.command(name="bankpanel")
@commands.has_permissions(administrator=True)
async def bankpanel(ctx: commands.Context):
    embed = discord.Embed(title="🏦 PAL BANK")
    await ctx.send(embed=embed, view=BankPanelView())


@bot.command(name="grant")
@commands.has_permissions(administrator=True)
async def grant(ctx: commands.Context, member: discord.Member, currency: str, amount: int):
    """管理者用: 指定ユーザーへPALまたはCHIPを付与する"""
    currency = currency.upper()

    if currency not in ("PAL", "CHIP"):
        await ctx.send("通貨はPALかCHIPのみ指定できます。")
        return

    if amount <= 0:
        await ctx.send("金額は1以上の整数で指定してください。")
        return

    user_id = str(member.id)
    await ensure_user_accounts(user_id)
    to_account_id = await get_user_account_id(user_id, currency)

    idempotency_key = f"ADMIN_GRANT:{uuid.uuid4()}"

    try:
        await transfer(
            idempotency_key=idempotency_key,
            transaction_type="ADMIN_GRANT",
            currency=currency,
            from_account_id=None,
            to_account_id=to_account_id,
            amount=amount,
            external_bot="ADMIN",
            external_reference_id=idempotency_key,
        )
    except AlreadyProcessedError:
        await ctx.send("この処理はすでに実行済みです。")
        return
    except InsufficientBalanceError:
        await ctx.send("残高処理でエラーが発生しました。")
        return

    await ctx.send(f"✅ {member.mention} に {amount:,} {currency} を付与しました。")


async def main():
    await init_db_pool()
    logger.info("DB接続・schema確認完了")

    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
