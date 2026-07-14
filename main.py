import os
import logging

import discord
from discord.ext import commands

from db import init_db_pool
from views import BankPanelView

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


async def main():
    await init_db_pool()
    logger.info("DB接続・schema確認完了")

    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
