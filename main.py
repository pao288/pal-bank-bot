import asyncio
import logging
import os

import discord
from discord.ext import commands

from db import get_pool, get_setting, init_db_pool, set_setting
from bank_services import rankings
from bank_advanced import maintenance_enabled, statistics
from views import AdminPanelView, BankPanelView, BankSetupPanelView, EnvelopeClaimView, ReviewView

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
            "🔄 **通貨交換**　PAL / CHIP交換\n"
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


async def restore_review_views():
    pool=get_pool()
    async with pool.acquire() as c:
        rows=await c.fetch("SELECT request_id FROM bank.transfer_requests WHERE status='PENDING'")
    for r in rows: bot.add_view(ReviewView(r["request_id"]))

def rank_embed(title,rows,suffix,rate=None):
    e=discord.Embed(title=title,color=0xFEE75C);medals={1:"🥇",2:"🥈",3:"🥉"};lines=[]
    previous=None;rank=0
    for n,r in enumerate(rows,1):
        if previous is None or r["balance"] != previous: rank=n
        previous=r["balance"];mark=medals.get(rank,f"`#{rank}`")
        lines.append(f"{mark} <@{r['owner_id']}> — **{r['balance']:,} {suffix}**")
    e.description="\n".join(lines) if lines else "データなし"
    footer="PAL BANK • 1時間ごとに自動更新"
    if rate is not None: footer += f" • 1 CHIP = {rate:,} PAL換算"
    e.set_footer(text=footer);return e

async def update_ranking():
    cid=await get_setting("ranking_channel_id")
    if not cid:return
    ch=bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid));pal,chip,total=await rankings()
    rate=int(await get_setting("chip_rate_pal","100"))
    embeds=[rank_embed("💰 PAL RANKING",pal,"PAL"),rank_embed("🎰 CHIP RANKING",chip,"CHIP"),rank_embed("🏆 TOTAL ASSET RANKING",total,"PAL換算",rate)]
    mid=await get_setting("ranking_message_id")
    if mid:
        try:msg=await ch.fetch_message(int(mid));await msg.edit(embeds=embeds);return
        except:pass
    msg=await ch.send(embeds=embeds);await set_setting("ranking_message_id",str(msg.id))

async def ranking_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:await update_ranking()
        except Exception:logger.exception("ranking update failed")
        await asyncio.sleep(3600)


async def movement_log_loop():
    await bot.wait_until_ready()
    last_id = 0
    pool = get_pool()
    async with pool.acquire() as conn:
        last_id = await conn.fetchval(
            "SELECT COALESCE(MAX(bank_transaction_id),0) FROM bank.transactions"
        )
    while not bot.is_closed():
        try:
            channel_id = await get_setting("movement_log_channel_id", "0")
            if channel_id and channel_id != "0":
                channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT t.*,fa.owner_id from_user,ta.owner_id to_user
                           FROM bank.transactions t
                           LEFT JOIN bank.accounts fa ON fa.account_id=t.from_account_id
                           LEFT JOIN bank.accounts ta ON ta.account_id=t.to_account_id
                           WHERE t.bank_transaction_id>$1 AND t.status='COMPLETED'
                           ORDER BY t.bank_transaction_id ASC LIMIT 100""",
                        last_id,
                    )
                for r in rows:
                    meta = r["metadata"] or {}
                    txid = meta.get("public_tx_id") or f"DB-{r['bank_transaction_id']}"
                    source = (r["external_bot"] or r["transaction_type"] or "BANK").upper()
                    if "SHOP" in source: icon, label = "🛒", "SHOP"
                    elif "CASINO" in source: icon, label = "🎰", "CASINO"
                    elif "VOICE" in source: icon, label = "🎙️", "VOICE"
                    elif "ADMIN" in source: icon, label = "🛠️", "ADMIN"
                    elif "TRANSFER" in r["transaction_type"]: icon, label = "💸", "SEND"
                    else: icon, label = "🏦", "BANK"
                    e = discord.Embed(title=f"{icon} {label}｜通貨移動", color=0x2B2D31)
                    e.description = (
                        f"取引ID: `{txid}`\n"
                        f"種類: `{r['transaction_type']}`\n"
                        f"通貨: **{r['amount']:,} {r['currency']}**\n"
                        f"FROM: {f'<@{r['from_user']}>' if r['from_user'] else 'SYSTEM'}\n"
                        f"TO: {f'<@{r['to_user']}>' if r['to_user'] else 'SYSTEM'}\n"
                        f"理由: {meta.get('reason') or '-'}"
                    )
                    await channel.send(embed=e)
                    last_id = r["bank_transaction_id"]
        except Exception:
            logger.exception("movement log update failed")
        await asyncio.sleep(15)


async def update_bank_status():
    channel_id = await get_setting("bank_status_channel_id", "0")
    if not channel_id or channel_id == "0":
        return
    channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
    stats = await statistics()
    rate = int(await get_setting("chip_rate_pal", "100"))
    fee = int(await get_setting("exchange_fee_percent", "0"))
    maintenance = await maintenance_enabled()
    e = discord.Embed(
        title="🏦 PAL BANK STATUS",
        color=0xED4245 if maintenance else 0x57F287,
    )
    e.description = "🔴 **MAINTENANCE**" if maintenance else "🟢 **BANK ONLINE**"
    e.add_field(name="交換レート", value=f"1 CHIP = **{rate:,} PAL**")
    e.add_field(name="交換手数料", value=f"**{fee}%**")
    e.add_field(name="口座ユーザー", value=f"**{stats['users']:,}人**")
    e.add_field(name="24時間取引", value=f"**{stats['tx24']:,}件**")
    e.set_footer(text="PAL BANK • 5分ごとに自動更新")
    message_id = await get_setting("bank_status_message_id", "0")
    if message_id and message_id != "0":
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(embed=e)
            return
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass
    msg = await channel.send(embed=e)
    await set_setting("bank_status_message_id", str(msg.id))


async def bank_status_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await update_bank_status()
        except Exception:
            logger.exception("bank status update failed")
        await asyncio.sleep(300)


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
    bot.add_view(BankSetupPanelView())
    await restore_envelope_views()
    await restore_review_views()
    await setup_fixed_panels()
    bot.loop.create_task(ranking_loop())
    bot.loop.create_task(movement_log_loop())
    bot.loop.create_task(bank_status_loop())

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



@bot.command(name="rankingpanel")
@commands.has_permissions(administrator=True)
async def rankingpanel(ctx: commands.Context):
    await set_setting("ranking_channel_id",str(ctx.channel.id))
    await set_setting("ranking_message_id","0")
    await update_ranking()
    await ctx.send("✅ ランキングパネル設置完了",delete_after=5)


@bot.command(name="banksetup")
@commands.has_permissions(administrator=True)
async def banksetup(ctx: commands.Context):
    embed = discord.Embed(title="⚙️ PAL BANK CHANNEL SETUP", color=0x2B2D31)
    embed.description = (
        "各ボタンは **ON / OFF式** です。\n"
        "未作成なら専用テキストチャンネルを新規作成。\n"
        "作成済みならその専用チャンネルを削除します。\n"
        "同じ種類のチャンネルは1個だけ管理します。"
    )
    await ctx.send(embed=embed, view=BankSetupPanelView())


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
