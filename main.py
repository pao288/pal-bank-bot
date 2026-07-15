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


# ===== !banksetup: PAL BANK カテゴリ／チャンネル／パネル 自動構築 =====
BANK_CATEGORY_NAME = "🏦 PAL BANK"
BANK_CATEGORY_SETTING_KEY = "pal_bank_category_id"

# (設定キー, チャンネル名, トピック)
# ranking_channel_id と movement_log_channel_id は既存機能（!rankingpanel /
# 送金審査チャンネルON-OFFパネル等）と同じ設定キーを再利用し、同一チャンネルとして扱う。
BANK_CHANNEL_DEFS = [
    ("bank_panel_channel_id", "💰｜銀行", "PAL BANK｜残高確認・送金・交換などのバンクパネル"),
    ("ranking_channel_id", "📊｜ランキング", "PAL BANK｜PAL・CHIP・総資産ランキング（自動更新）"),
    ("movement_log_channel_id", "📜｜ログ", "PAL BANK｜通貨移動ログ"),
    ("admin_panel_channel_id", "🛠｜管理", "PAL BANK｜管理者専用コンソール"),
]


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


def _bank_overwrites(guild: discord.Guild) -> dict:
    # 一般ユーザー: 閲覧可能・送信不可 / BOTと管理者(Administrator権限保持者)は通常通り操作可能
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
            add_reactions=True,
        ),
    }


async def ensure_bank_category(guild: discord.Guild) -> discord.CategoryChannel:
    overwrites = _bank_overwrites(guild)
    category_id = await get_setting(BANK_CATEGORY_SETTING_KEY, "0")
    category = None
    if category_id and category_id != "0":
        found = guild.get_channel(int(category_id))
        if isinstance(found, discord.CategoryChannel):
            category = found
    if category is None:
        # 既に存在するものは再利用（名前一致）、不足していれば新規作成
        category = discord.utils.get(guild.categories, name=BANK_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(
            BANK_CATEGORY_NAME, overwrites=overwrites, reason="PAL BANK setup"
        )
    else:
        try:
            await category.edit(overwrites=overwrites)
        except discord.HTTPException:
            logger.exception("カテゴリ権限更新失敗: %s", category.id)
    await set_setting(BANK_CATEGORY_SETTING_KEY, str(category.id))
    return category


async def ensure_bank_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    setting_key: str,
    name: str,
    topic: str,
) -> discord.TextChannel:
    overwrites = _bank_overwrites(guild)
    channel_id = await get_setting(setting_key, "0")
    channel = None
    if channel_id and channel_id != "0":
        found = guild.get_channel(int(channel_id))
        if isinstance(found, discord.TextChannel):
            channel = found
    if channel is None:
        # 既に存在するものは再利用（カテゴリ内の名前一致）、不足していれば新規作成
        channel = discord.utils.get(category.text_channels, name=name)
    if channel is None:
        channel = await guild.create_text_channel(
            name=name,
            category=category,
            topic=topic,
            overwrites=overwrites,
            reason="PAL BANK setup",
        )
    else:
        try:
            if channel.category_id != category.id:
                await channel.edit(category=category, sync_permissions=False)
            await channel.edit(overwrites=overwrites)
        except discord.HTTPException:
            logger.exception("チャンネル権限更新失敗: %s", channel.id)
    await set_setting(setting_key, str(channel.id))
    return channel


async def refresh_panels(guild: discord.Guild) -> list[str]:
    """既存チャンネルに対して、銀行パネル／ランキングパネル／管理パネルのみを再設置する。"""
    posted = []

    bank_channel_id = await get_setting("bank_panel_channel_id", "0")
    if bank_channel_id and bank_channel_id != "0" and guild.get_channel(int(bank_channel_id)):
        await ensure_fixed_panel(int(bank_channel_id), "user")
        posted.append("💰 銀行パネル")

    admin_channel_id = await get_setting("admin_panel_channel_id", "0")
    if admin_channel_id and admin_channel_id != "0" and guild.get_channel(int(admin_channel_id)):
        await ensure_fixed_panel(int(admin_channel_id), "admin")
        posted.append("🛠 管理パネル")

    ranking_channel_id = await get_setting("ranking_channel_id", "0")
    if ranking_channel_id and ranking_channel_id != "0" and guild.get_channel(int(ranking_channel_id)):
        await update_ranking()
        posted.append("📊 ランキングパネル")

    return posted


async def ensure_bank_system(guild: discord.Guild):
    """!banksetup 本体。既存のカテゴリ／チャンネルは再利用し、不足分のみ作成した上でパネルを設置する。
    DB（残高・履歴・口座・ランキング等）には一切触れない。"""
    category = await ensure_bank_category(guild)
    channels = {}
    for setting_key, name, topic in BANK_CHANNEL_DEFS:
        channels[setting_key] = await ensure_bank_channel(guild, category, setting_key, name, topic)
    await refresh_panels(guild)
    return category, channels


async def delete_bank_system(guild: discord.Guild) -> list[str]:
    """Discord側（カテゴリ・チャンネル・パネル）のみを削除する。銀行DBのデータは一切削除しない。"""
    deleted = []

    for setting_key, name, _topic in BANK_CHANNEL_DEFS:
        channel_id = await get_setting(setting_key, "0")
        if channel_id and channel_id != "0":
            channel = guild.get_channel(int(channel_id))
            if channel is not None:
                try:
                    await channel.delete(reason="PAL BANK system delete (Discord側のみ)")
                    deleted.append(name)
                except discord.HTTPException:
                    logger.exception("チャンネル削除失敗: %s", channel_id)
        await set_setting(setting_key, "0")

    category_id = await get_setting(BANK_CATEGORY_SETTING_KEY, "0")
    if category_id and category_id != "0":
        category = guild.get_channel(int(category_id))
        if category is not None:
            try:
                await category.delete(reason="PAL BANK system delete (Discord側のみ)")
                deleted.append(category.name)
            except discord.HTTPException:
                logger.exception("カテゴリ削除失敗: %s", category_id)
    await set_setting(BANK_CATEGORY_SETTING_KEY, "0")

    # パネルメッセージの追跡idもリセット（メッセージ自体はチャンネル削除で消滅済み）
    for key in ("user_panel_message_id", "admin_panel_message_id", "ranking_message_id"):
        await set_setting(key, "0")

    return deleted


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


# 元々 !banksetup という名前だった個別チャンネルON/OFFトグル機能。
# 新しい !banksetup（PAL BANKカテゴリ一式の自動構築）とコマンド名が重複するため、
# 機能は一切変更せずに !bankchannels という名前へ変更のみ行っている。
@bot.command(name="bankchannels")
@commands.has_permissions(administrator=True)
async def bankchannels(ctx: commands.Context):
    embed = discord.Embed(title="⚙️ PAL BANK CHANNEL SETUP", color=0x2B2D31)
    embed.description = (
        "各ボタンは **ON / OFF式** です。\n"
        "未作成なら専用テキストチャンネルを新規作成。\n"
        "作成済みならその専用チャンネルを削除します。\n"
        "同じ種類のチャンネルは1個だけ管理します。"
    )
    await ctx.send(embed=embed, view=BankSetupPanelView())


@bot.command(name="banksetup")
@commands.has_permissions(administrator=True)
async def banksetup(ctx: commands.Context):
    """🏦 PAL BANK カテゴリ・チャンネル（💰銀行/📊ランキング/📜ログ/🛠管理）・
    各種パネルを一括で自動構築する。既に存在するものは再利用し、不足分のみ作成する。
    再実行時はDiscord側で削除された部分のみ復元し、DBデータはそのまま利用する。"""
    if ctx.guild is None:
        await ctx.send("サーバー内で実行してください。", delete_after=5)
        return

    message = await ctx.send("🏦 PAL BANK システムをセットアップしています…")
    try:
        category, channels = await ensure_bank_system(ctx.guild)
    except discord.Forbidden:
        await message.edit(content="❌ 権限不足でチャンネル/カテゴリを作成できませんでした。BOTの権限を確認してください。")
        return
    except Exception:
        logger.exception("banksetup failed")
        await message.edit(content="❌ セットアップ中にエラーが発生しました。ログを確認してください。")
        return

    bank_ch = channels["bank_panel_channel_id"]
    ranking_ch = channels["ranking_channel_id"]
    log_ch = channels["movement_log_channel_id"]
    admin_ch = channels["admin_panel_channel_id"]

    await message.edit(
        content=(
            "✅ PAL BANK セットアップ完了！\n"
            f"カテゴリ: **{category.name}**\n"
            f"💰 銀行: {bank_ch.mention}\n"
            f"📊 ランキング: {ranking_ch.mention}\n"
            f"📜 ログ: {log_ch.mention}\n"
            f"🛠 管理: {admin_ch.mention}\n\n"
            "一般ユーザーは閲覧のみ可能（送信不可）、BOTと管理者は通常通り操作できます。"
        )
    )


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
