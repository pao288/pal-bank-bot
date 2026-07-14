import logging
import os

import discord
from discord.ext import commands

from db import get_pool, get_setting, init_db_pool, set_setting
from views import AdminPanelView, BankPanelView, EnvelopeClaimView

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pal_bank")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
_ready_once = False


def user_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏦 PAL BANK｜DIGITAL WALLET",
        description=(
            "**PALとCHIPを、ここから管理。**\n\n"
            "💰 **残高確認**　現在のPAL / CHIPを表示\n"
            "💸 **送金**　ユーザーへPALを送金\n"
            "🧧 **ポチ袋作成**　PALをランダム配布\n"
            "📖 **取引履歴**　最新の入出金を確認\n\n"
            "操作したい機能を下のボタンから選択してください。"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="PAL BANK • PAL & CHIP WALLET")
    return embed


def admin_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔧 PAL BANK｜ADMIN CONSOLE",
        description=(
            "**BANK管理者専用コンソール**\n\n"
            "PAL / CHIPの付与・回収、ユーザー残高照会、"
            "BANK全体の取引履歴を管理できます。\n\n"
            "実行する管理操作を下のボタンから選択してください。"
        ),
        color=0x2B2D31,
    )
    embed.set_footer(text="PAL BANK • ADMIN CONSOLE")
    return embed


async def restore_envelope_views():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT envelope_id FROM bank.pal_envelopes WHERE status='ACTIVE'"
        )
    for row in rows:
        bot.add_view(EnvelopeClaimView(row["envelope_id"]))


async def ensure_fixed_panel(channel_id: int, kind: str):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.exception("固定パネルのチャンネル取得失敗: %s", channel_id)
            return

    key = f"{kind}_panel_message_id"
    old_id = await get_setting(key)

    if old_id:
        try:
            message = await channel.fetch_message(int(old_id))
            if kind == "user":
                await message.edit(embed=user_panel_embed(), view=BankPanelView())
            else:
                await message.edit(embed=admin_panel_embed(), view=AdminPanelView())
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass

    if kind == "user":
        message = await channel.send(embed=user_panel_embed(), view=BankPanelView())
    else:
        message = await channel.send(embed=admin_panel_embed(), view=AdminPanelView())
    await set_setting(key, str(message.id))


async def setup_fixed_panels():
    user_channel = os.getenv("BANK_PANEL_CHANNEL_ID")
    admin_channel = os.getenv("BANK_ADMIN_CHANNEL_ID")

    if user_channel:
        await ensure_fixed_panel(int(user_channel), "user")
    if admin_channel:
        await ensure_fixed_panel(int(admin_channel), "admin")


@bot.event
async def on_ready():
    global _ready_once
    if _ready_once:
        logger.info("PAL BANK BOT再接続: %s", bot.user)
        return

    bot.add_view(BankPanelView())
    bot.add_view(AdminPanelView())
    await restore_envelope_views()
    await setup_fixed_panels()

    _ready_once = True
    logger.info("PAL BANK BOT起動完了: %s", bot.user)


# 初回設置・復旧用の管理コマンド。通常ユーザーは固定パネルのみ利用。
@bot.command(name="bankpanel")
@commands.has_permissions(administrator=True)
async def bankpanel(ctx: commands.Context):
    message = await ctx.send(embed=user_panel_embed(), view=BankPanelView())
    await set_setting("user_panel_message_id", str(message.id))


@bot.command(name="adminpanel")
@commands.has_permissions(administrator=True)
async def adminpanel(ctx: commands.Context):
    message = await ctx.send(embed=admin_panel_embed(), view=AdminPanelView())
    await set_setting("admin_panel_message_id", str(message.id))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("管理者専用です.", delete_after=5)
        return
    if isinstance(error, commands.CommandNotFound):
        return
    logger.exception("Command error", exc_info=error)


async def main():
    await init_db_pool()
    logger.info("DB接続・schema確認完了")
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
