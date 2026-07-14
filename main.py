import logging
import os

import discord
from discord.ext import commands

from db import get_pool, init_db_pool
from views import AdminPanelView, BankPanelView, EnvelopeClaimView

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pal_bank")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def restore_envelope_views():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT envelope_id
            FROM bank.pal_envelopes
            WHERE status='ACTIVE'
            """
        )
    for row in rows:
        bot.add_view(EnvelopeClaimView(row["envelope_id"]))


@bot.event
async def on_ready():
    bot.add_view(BankPanelView())
    bot.add_view(AdminPanelView())
    await restore_envelope_views()
    logger.info("PAL BANK BOT起動完了: %s", bot.user)


# 固定パネルの初回設置用。通常ユーザーはコマンド操作しない。
@bot.command(name="bankpanel")
@commands.has_permissions(administrator=True)
async def bankpanel(ctx: commands.Context):
    embed = discord.Embed(
        title="🏦 PAL BANK",
        description="下のボタンからBANK機能を利用できます。",
    )
    await ctx.send(embed=embed, view=BankPanelView())


@bot.command(name="adminpanel")
@commands.has_permissions(administrator=True)
async def adminpanel(ctx: commands.Context):
    embed = discord.Embed(
        title="🔧 PAL BANK 管理パネル",
        description="管理操作を選択してください。",
    )
    await ctx.send(embed=embed, view=AdminPanelView())


async def main():
    await init_db_pool()
    logger.info("DB接続・schema確認完了")
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
