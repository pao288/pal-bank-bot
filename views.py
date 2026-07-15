import random
import uuid

import discord

from accounts import ensure_user_accounts, get_user_account_id, get_user_balances
from db import get_pool, get_setting, set_setting
from transactions import InsufficientBalanceError, get_all_history, get_history, transfer
from bank_services import exchange, totals

PAL_BLUE = 0x5865F2
PAL_GREEN = 0x57F287
PAL_GOLD = 0xFEE75C
PAL_RED = 0xED4245
PAL_DARK = 0x2B2D31


def bank_embed(title: str, description: str | None = None, color: int = PAL_BLUE) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="PAL BANK • PAL & CHIP WALLET")
    return embed


def money(value: int, currency: str) -> str:
    return f"{value:,} {currency}"


def history_embed(title: str, rows: list, viewer_id: str | None = None) -> discord.Embed:
    embed = bank_embed(title, color=PAL_DARK)
    if not rows:
        embed.description = "取引履歴はありません。"
        return embed

    lines = []
    for row in rows[:40]:
        from_id = row["from_owner_id"]
        to_id = row["to_owner_id"]
        if viewer_id and to_id == viewer_id and from_id != viewer_id:
            mark = "📥"
        elif viewer_id and from_id == viewer_id and to_id != viewer_id:
            mark = "📤"
        else:
            mark = "🔄"
        lines.append(
            f"{mark} `#{row['bank_transaction_id']}` "
            f"**{row['amount']:,} {row['currency']}** • {row['transaction_type']}"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"PAL BANK • 最新{min(len(rows), 40)}件")
    return embed


class TransferAmountModal(discord.ui.Modal, title="💸 PAL送金"):
    amount = discord.ui.TextInput(label="送金額", placeholder="例: 10000", max_length=18)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.amount).replace(",", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("金額は1以上の数字で入力してください。", ephemeral=True)
            return

        sender_id = str(interaction.user.id)
        target_id = str(self.target.id)
        if sender_id == target_id:
            await interaction.response.send_message("自分自身には送金できません。", ephemeral=True)
            return

        from_id = await get_user_account_id(sender_id, "PAL")
        to_id = await get_user_account_id(target_id, "PAL")
        key = f"USER_TRANSFER:{interaction.id}:{uuid.uuid4()}"

        try:
            await transfer(
                key, "USER_TRANSFER", "PAL", from_id, to_id, amount,
                external_bot="PAL_BANK",
                external_reference_id=str(interaction.id),
                metadata={"sender_id": sender_id, "target_id": target_id},
            )
        except InsufficientBalanceError:
            await interaction.response.send_message("PAL残高が足りません。", ephemeral=True)
            return

        embed = bank_embed("✅ TRANSFER COMPLETE", color=PAL_GREEN)
        embed.description = f"{self.target.mention} に **{amount:,} PAL** 送金しました。"
        await interaction.response.send_message(embed=embed, ephemeral=True)


class TransferUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="送金相手を選択", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if target.bot:
            await interaction.response.send_message("Botは選択できません。", ephemeral=True)
            return
        await interaction.response.send_modal(TransferAmountModal(target))


class TransferUserView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TransferUserSelect())


class EnvelopeModal(discord.ui.Modal, title="🧧 PALポチ袋作成"):
    total_amount = discord.ui.TextInput(label="合計PAL", placeholder="例: 10000", max_length=18)
    max_claims = discord.ui.TextInput(label="受取人数", placeholder="1〜100", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            total = int(str(self.total_amount).replace(",", "").strip())
            claims = int(str(self.max_claims).strip())
            if total <= 0 or claims <= 0 or claims > 100 or total < claims:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "合計PALは1以上、人数は1〜100人、合計PALは人数以上で入力してください。",
                ephemeral=True,
            )
            return

        creator_id = str(interaction.user.id)
        source_id = await get_user_account_id(creator_id, "PAL")
        cuts = sorted(random.sample(range(1, total), claims - 1)) if claims > 1 else []
        points = [0] + cuts + [total]
        slots = [points[i + 1] - points[i] for i in range(claims)]
        random.shuffle(slots)

        pool = get_pool()
        key = f"ENVELOPE_CREATE:{interaction.id}:{uuid.uuid4()}"

        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    balance = await conn.fetchval(
                        "SELECT balance FROM bank.accounts WHERE account_id=$1 FOR UPDATE",
                        source_id,
                    )
                    if balance is None or balance < total:
                        raise InsufficientBalanceError(key)

                    envelope_id = await conn.fetchval(
                        """
                        INSERT INTO bank.pal_envelopes
                        (creator_id,total_amount,max_claims,status,source_channel_id)
                        VALUES ($1,$2,$3,'ACTIVE',$4)
                        RETURNING envelope_id
                        """,
                        creator_id, total, claims, str(interaction.channel_id),
                    )
                    await conn.execute(
                        "UPDATE bank.accounts SET balance=balance-$1,updated_at=now() WHERE account_id=$2",
                        total, source_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO bank.transactions (
                            idempotency_key,transaction_type,currency,
                            from_account_id,to_account_id,amount,
                            external_bot,external_reference_id,status,metadata,completed_at
                        )
                        VALUES ($1,'PAL_ENVELOPE_CREATE','PAL',$2,NULL,$3,
                                'PAL_BANK',$4,'COMPLETED',
                                jsonb_build_object('envelope_id',$5::bigint),now())
                        """,
                        key, source_id, total, str(envelope_id), envelope_id,
                    )
                    await conn.executemany(
                        """
                        INSERT INTO bank.pal_envelope_slots(envelope_id,amount,slot_order)
                        VALUES ($1,$2,$3)
                        """,
                        [(envelope_id, value, i + 1) for i, value in enumerate(slots)],
                    )
        except InsufficientBalanceError:
            await interaction.response.send_message("PAL残高が足りません。", ephemeral=True)
            return

        embed = bank_embed("🧧 PAL ENVELOPE", color=PAL_GOLD)
        embed.description = (
            f"{interaction.user.mention} からPALポチ袋！\n\n"
            f"💰 **合計 {total:,} PAL**\n"
            f"👥 **{claims}人限定**\n\n"
            "下のボタンから受け取ってください。"
        )
        await interaction.response.send_message("ポチ袋を作成しました。", ephemeral=True)
        message = await interaction.channel.send(embed=embed, view=EnvelopeClaimView(envelope_id))
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE bank.pal_envelopes SET source_message_id=$1 WHERE envelope_id=$2",
                str(message.id), envelope_id,
            )


class EnvelopeClaimView(discord.ui.View):
    def __init__(self, envelope_id: int):
        super().__init__(timeout=None)
        self.envelope_id = envelope_id
        button = discord.ui.Button(
            label="🧧 受け取る",
            style=discord.ButtonStyle.success,
            custom_id=f"bank_envelope_claim:{envelope_id}",
        )
        button.callback = self.claim
        self.add_item(button)

    async def claim(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        to_id = await get_user_account_id(user_id, "PAL")
        pool = get_pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                envelope = await conn.fetchrow(
                    "SELECT * FROM bank.pal_envelopes WHERE envelope_id=$1 FOR UPDATE",
                    self.envelope_id,
                )
                if envelope is None or envelope["status"] != "ACTIVE":
                    await interaction.response.send_message("このポチ袋は終了しています。", ephemeral=True)
                    return

                if await conn.fetchval(
                    "SELECT 1 FROM bank.pal_envelope_claims WHERE envelope_id=$1 AND user_id=$2",
                    self.envelope_id, user_id,
                ):
                    await interaction.response.send_message("このポチ袋は受取済みです。", ephemeral=True)
                    return

                slot = await conn.fetchrow(
                    """
                    SELECT slot_id,amount
                    FROM bank.pal_envelope_slots
                    WHERE envelope_id=$1 AND claimed_by IS NULL
                    ORDER BY random()
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    self.envelope_id,
                )
                if slot is None:
                    await conn.execute(
                        "UPDATE bank.pal_envelopes SET status='COMPLETED',completed_at=now() WHERE envelope_id=$1",
                        self.envelope_id,
                    )
                    await interaction.response.send_message("このポチ袋は終了しています。", ephemeral=True)
                    return

                await conn.execute(
                    "UPDATE bank.pal_envelope_slots SET claimed_by=$1,claimed_at=now() WHERE slot_id=$2",
                    user_id, slot["slot_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO bank.pal_envelope_claims(envelope_id,user_id,slot_id,amount)
                    VALUES ($1,$2,$3,$4)
                    """,
                    self.envelope_id, user_id, slot["slot_id"], slot["amount"],
                )
                await conn.execute(
                    "UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",
                    slot["amount"], to_id,
                )
                await conn.execute(
                    """
                    INSERT INTO bank.transactions (
                        idempotency_key,transaction_type,currency,
                        from_account_id,to_account_id,amount,
                        external_bot,external_reference_id,status,metadata,completed_at
                    )
                    VALUES ($1,'PAL_ENVELOPE_CLAIM','PAL',NULL,$2,$3,
                            'PAL_BANK',$4,'COMPLETED',
                            jsonb_build_object('envelope_id',$5::bigint),now())
                    """,
                    f"ENVELOPE_CLAIM:{self.envelope_id}:{user_id}",
                    to_id, slot["amount"], str(self.envelope_id), self.envelope_id,
                )
                count = await conn.fetchval(
                    """
                    UPDATE bank.pal_envelopes
                    SET claimed_count=claimed_count+1
                    WHERE envelope_id=$1
                    RETURNING claimed_count
                    """,
                    self.envelope_id,
                )
                completed = count >= envelope["max_claims"]
                if completed:
                    await conn.execute(
                        "UPDATE bank.pal_envelopes SET status='COMPLETED',completed_at=now() WHERE envelope_id=$1",
                        self.envelope_id,
                    )

        await interaction.response.send_message(
            f"🧧 **{slot['amount']:,} PAL** 受け取りました！",
            ephemeral=True,
        )
        if completed and interaction.message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass


class LegacyBankPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 残高確認", style=discord.ButtonStyle.primary, custom_id="bank_check_balance", row=0)
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        balances = await get_user_balances(str(interaction.user.id))
        embed = bank_embed("🏦 PAL BANK｜MY ACCOUNT", "あなた専用の口座情報です。")
        embed.add_field(name="💰 PAL BALANCE", value=f"**{money(balances['PAL'], 'PAL')}**", inline=True)
        embed.add_field(name="🎰 CHIP BALANCE", value=f"**{money(balances['CHIP'], 'CHIP')}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="💸 送金", style=discord.ButtonStyle.secondary, custom_id="bank_transfer", row=0)
    async def transfer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("送金相手を選んでください。", view=TransferUserView(), ephemeral=True)

    @discord.ui.button(label="🧧 ポチ袋作成", style=discord.ButtonStyle.secondary, custom_id="bank_envelope_create", row=0)
    async def envelope_create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EnvelopeModal())

    @discord.ui.button(label="📖 取引履歴", style=discord.ButtonStyle.secondary, custom_id="bank_history", row=1)
    async def history_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        rows = await get_history(user_id, 100)
        await interaction.response.send_message(
            embed=history_embed("📖 TRANSACTION HISTORY", rows, user_id),
            ephemeral=True,
        )


class AdminAmountModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="金額", placeholder="例: 10000", max_length=18)

    def __init__(self, target: discord.Member, currency: str, action: str):
        self.target = target
        self.currency = currency
        self.action = action
        super().__init__(title=f"{currency} {'付与' if action == 'GRANT' else '回収'}")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(str(self.amount).replace(",", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("1以上の数字を入力してください。", ephemeral=True)
            return

        account_id = await get_user_account_id(str(self.target.id), self.currency)
        key = f"ADMIN_{self.action}:{interaction.id}:{uuid.uuid4()}"

        try:
            await transfer(
                key,
                f"ADMIN_{self.action}",
                self.currency,
                account_id if self.action == "TAKE" else None,
                account_id if self.action == "GRANT" else None,
                amount,
                external_bot="PAL_BANK_ADMIN",
                external_reference_id=str(interaction.id),
                metadata={"admin_id": str(interaction.user.id), "target_id": str(self.target.id)},
            )
        except InsufficientBalanceError:
            await interaction.response.send_message("対象ユーザーの残高が足りません。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ {self.target.mention} / **{amount:,} {self.currency}** / "
            f"{'付与' if self.action == 'GRANT' else '回収'}完了",
            ephemeral=True,
        )


class AdminTargetSelect(discord.ui.UserSelect):
    def __init__(self, mode: str, currency: str | None = None, action: str | None = None):
        super().__init__(placeholder="対象ユーザーを選択", min_values=1, max_values=1)
        self.mode = mode
        self.currency = currency
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if self.mode == "BALANCE":
            balances = await get_user_balances(str(target.id))
            embed = bank_embed(f"🏦 USER ACCOUNT｜{target.display_name}", "管理者用残高照会", PAL_DARK)
            embed.add_field(name="💰 PAL", value=f"**{money(balances['PAL'], 'PAL')}**", inline=True)
            embed.add_field(name="🎰 CHIP", value=f"**{money(balances['CHIP'], 'CHIP')}**", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_modal(AdminAmountModal(target, self.currency, self.action))


class AdminTargetView(discord.ui.View):
    def __init__(self, mode: str, currency: str | None = None, action: str | None = None):
        super().__init__(timeout=120)
        self.add_item(AdminTargetSelect(mode, currency, action))


class LegacyAdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("管理者専用パネルです。", ephemeral=True)
            return False
        return True

    async def _target(self, interaction, currency, action):
        await interaction.response.send_message(
            "対象ユーザーを選んでください。",
            view=AdminTargetView("MONEY", currency, action),
            ephemeral=True,
        )

    @discord.ui.button(label="PAL付与", style=discord.ButtonStyle.success, custom_id="admin_pal_grant", row=0)
    async def pal_grant(self, interaction, button):
        await self._target(interaction, "PAL", "GRANT")

    @discord.ui.button(label="PAL回収", style=discord.ButtonStyle.danger, custom_id="admin_pal_take", row=0)
    async def pal_take(self, interaction, button):
        await self._target(interaction, "PAL", "TAKE")

    @discord.ui.button(label="CHIP付与", style=discord.ButtonStyle.success, custom_id="admin_chip_grant", row=1)
    async def chip_grant(self, interaction, button):
        await self._target(interaction, "CHIP", "GRANT")

    @discord.ui.button(label="CHIP回収", style=discord.ButtonStyle.danger, custom_id="admin_chip_take", row=1)
    async def chip_take(self, interaction, button):
        await self._target(interaction, "CHIP", "TAKE")

    @discord.ui.button(label="ユーザー残高確認", style=discord.ButtonStyle.secondary, custom_id="admin_balance", row=2)
    async def balance(self, interaction, button):
        await interaction.response.send_message(
            "対象ユーザーを選んでください。",
            view=AdminTargetView("BALANCE"),
            ephemeral=True,
        )

    @discord.ui.button(label="取引履歴確認", style=discord.ButtonStyle.secondary, custom_id="admin_history", row=2)
    async def history(self, interaction, button):
        rows = await get_all_history(100)
        await interaction.response.send_message(
            embed=history_embed("📖 BANK TRANSACTION LOG", rows),
            ephemeral=True,
        )


class ReviewView(discord.ui.View):
    def __init__(self, request_id:int):
        super().__init__(timeout=None); self.request_id=request_id
        a=discord.ui.Button(label="✅ 許可",style=discord.ButtonStyle.success,custom_id=f"review_ok:{request_id}")
        r=discord.ui.Button(label="❌ 却下",style=discord.ButtonStyle.danger,custom_id=f"review_no:{request_id}")
        a.callback=self.approve;r.callback=self.reject;self.add_item(a);self.add_item(r)
    async def interaction_check(self,i):
        if not i.user.guild_permissions.administrator:
            await i.response.send_message("管理者専用です。",ephemeral=True);return False
        return True
    async def approve(self,i):
        pool=get_pool()
        async with pool.acquire() as c:
          async with c.transaction():
            q=await c.fetchrow("SELECT * FROM bank.transfer_requests WHERE request_id=$1 FOR UPDATE",self.request_id)
            if not q or q["status"]!="PENDING":
                await i.response.send_message("審査済みです。",ephemeral=True);return
            fr=await c.fetchval("SELECT account_id FROM bank.accounts WHERE account_type='USER' AND owner_id=$1 AND currency='PAL'",q["requester_id"])
            to=await c.fetchval("SELECT account_id FROM bank.accounts WHERE account_type='USER' AND owner_id=$1 AND currency='PAL'",q["recipient_id"])
            ok=await c.fetchval("UPDATE bank.accounts SET balance=balance-$1,updated_at=now() WHERE account_id=$2 AND balance >= $1 RETURNING account_id",q["amount"],fr)
            if not ok:
                await c.execute("UPDATE bank.transfer_requests SET status='FAILED',reviewed_by=$1,reviewed_at=now() WHERE request_id=$2",str(i.user.id),self.request_id)
                await i.response.edit_message(content="残高不足で処理終了",embed=None,view=None);return
            await c.execute("UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",q["amount"],to)
            await c.execute("""INSERT INTO bank.transactions(idempotency_key,transaction_type,currency,from_account_id,to_account_id,amount,external_bot,external_reference_id,status,completed_at) VALUES($1,'USER_TRANSFER_APPROVED','PAL',$2,$3,$4,'PAL_BANK',$5,'COMPLETED',now())""",f"REVIEW:{self.request_id}",fr,to,q["amount"],str(self.request_id))
            await c.execute("UPDATE bank.transfer_requests SET status='APPROVED',reviewed_by=$1,reviewed_at=now() WHERE request_id=$2",str(i.user.id),self.request_id)
        await i.response.edit_message(content=f"✅ 許可済み｜審査者 {i.user.mention}",view=None)
    async def reject(self,i):
        pool=get_pool()
        async with pool.acquire() as c:
            result=await c.execute("UPDATE bank.transfer_requests SET status='REJECTED',reviewed_by=$1,reviewed_at=now() WHERE request_id=$2 AND status='PENDING'",str(i.user.id),self.request_id)
        if result.endswith("0"): await i.response.send_message("審査済みです。",ephemeral=True);return
        await i.response.edit_message(content=f"❌ 却下済み｜審査者 {i.user.mention}",embed=None,view=None)

class RequestAmountModal(discord.ui.Modal,title="💸 PAL送金申請"):
    amount=discord.ui.TextInput(label="送金額",placeholder="例: 10000",max_length=18)
    def __init__(self,target):super().__init__();self.target=target
    async def on_submit(self,i):
        try:
            amount=int(str(self.amount).replace(",",""));assert amount>0
        except: await i.response.send_message("1以上の数字を入力してください。",ephemeral=True);return
        b=await get_user_balances(str(i.user.id))
        if b["PAL"]<amount: await i.response.send_message("PAL残高が足りません。",ephemeral=True);return
        ch=await get_setting("transfer_review_channel_id")
        if not ch: await i.response.send_message("送金審査チャンネルが未設定です。",ephemeral=True);return
        pool=get_pool()
        async with pool.acquire() as c:
            rid=await c.fetchval("INSERT INTO bank.transfer_requests(requester_id,recipient_id,amount,status) VALUES($1,$2,$3,'PENDING') RETURNING request_id",str(i.user.id),str(self.target.id),amount)
        channel=i.client.get_channel(int(ch)) or await i.client.fetch_channel(int(ch))
        e=bank_embed(f"💸 PAL送金審査｜#{rid:06d}",color=PAL_GOLD)
        e.description=f"申請者: {i.user.mention}\n送金先: {self.target.mention}\n金額: **{amount:,} PAL**\n申請時残高: {b['PAL']:,} PAL"
        m=await channel.send(embed=e,view=ReviewView(rid))
        async with pool.acquire() as c: await c.execute("UPDATE bank.transfer_requests SET review_channel_id=$1,review_message_id=$2 WHERE request_id=$3",str(channel.id),str(m.id),rid)
        await i.response.send_message(f"送金申請 `#{rid:06d}` を運営へ送りました。",ephemeral=True)

class RequestUserSelect(discord.ui.UserSelect):
    def __init__(self):super().__init__(placeholder="送金相手を選択",min_values=1,max_values=1)
    async def callback(self,i):
        if self.values[0].bot: await i.response.send_message("Botは選択できません。",ephemeral=True);return
        await i.response.send_modal(RequestAmountModal(self.values[0]))
class RequestUserView(discord.ui.View):
    def __init__(self):super().__init__(timeout=120);self.add_item(RequestUserSelect())

class ExchangeModal2(discord.ui.Modal):
    amount=discord.ui.TextInput(label="交換額",placeholder="数字のみ",max_length=18)
    def __init__(self,d):self.d=d;super().__init__(title="PAL→CHIP" if d=="P2C" else "CHIP→PAL")
    async def on_submit(self,i):
        try:
            a=int(str(self.amount).replace(",",""));x,xc,y,yc=await exchange(str(i.user.id),self.d,a)
            await i.response.send_message(f"✅ **{x:,} {xc} → {y:,} {yc}**",ephemeral=True)
        except InsufficientBalanceError: await i.response.send_message("残高が足りません。",ephemeral=True)
        except ValueError as e: await i.response.send_message(str(e),ephemeral=True)
class ExchangeView2(discord.ui.View):
    def __init__(self):super().__init__(timeout=120)
    @discord.ui.button(label="PAL → CHIP",style=discord.ButtonStyle.primary)
    async def a(self,i,b):await i.response.send_modal(ExchangeModal2("P2C"))
    @discord.ui.button(label="CHIP → PAL",style=discord.ButtonStyle.secondary)
    async def b(self,i,b):await i.response.send_modal(ExchangeModal2("C2P"))

class SettingsModal(discord.ui.Modal,title="🔄 交換設定"):
    rate=discord.ui.TextInput(label="1 CHIP = 何PAL",placeholder="100")
    fee=discord.ui.TextInput(label="手数料%",placeholder="0")
    minimum=discord.ui.TextInput(label="最低交換PAL",placeholder="1000")
    async def on_submit(self,i):
        try:r=int(str(self.rate));f=int(str(self.fee));m=int(str(self.minimum));assert r>0 and 0<=f<=100 and m>0
        except:await i.response.send_message("数字を正しく入力してください。",ephemeral=True);return
        await set_setting("chip_rate_pal",str(r));await set_setting("exchange_fee_percent",str(f));await set_setting("exchange_min_pal",str(m))
        await i.response.send_message(f"✅ 1 CHIP={r:,} PAL / 手数料{f}% / 最低{m:,} PAL",ephemeral=True)

class BankPanelView(discord.ui.View):
    def __init__(self):super().__init__(timeout=None)
    @discord.ui.button(label="💰 残高確認",style=discord.ButtonStyle.primary,custom_id="final_balance",row=0)
    async def bal(self,i,b):
        x=await get_user_balances(str(i.user.id));e=bank_embed("🏦 PAL BANK｜MY ACCOUNT");e.add_field(name="💰 PAL",value=f"**{x['PAL']:,} PAL**");e.add_field(name="🎰 CHIP",value=f"**{x['CHIP']:,} CHIP**");await i.response.send_message(embed=e,ephemeral=True)
    @discord.ui.button(label="💸 送金申請",style=discord.ButtonStyle.secondary,custom_id="final_transfer",row=0)
    async def send(self,i,b):await i.response.send_message("送金相手を選択",view=RequestUserView(),ephemeral=True)
    @discord.ui.button(label="🧧 ポチ袋作成",style=discord.ButtonStyle.secondary,custom_id="final_envelope",row=0)
    async def env(self,i,b):await i.response.send_modal(EnvelopeModal())
    @discord.ui.button(label="🔄 通貨交換",style=discord.ButtonStyle.primary,custom_id="final_exchange",row=1)
    async def ex(self,i,b):
        r=await get_setting("chip_rate_pal","100");f=await get_setting("exchange_fee_percent","0");m=await get_setting("exchange_min_pal","1000")
        await i.response.send_message(f"**1 CHIP = {int(r):,} PAL｜手数料 {f}%｜最低 {int(m):,} PAL**",view=ExchangeView2(),ephemeral=True)
    @discord.ui.button(label="📖 取引履歴",style=discord.ButtonStyle.secondary,custom_id="final_history",row=1)
    async def hist(self,i,b):
        uid=str(i.user.id);rows=await get_history(uid,100);await i.response.send_message(embed=history_embed("📖 TRANSACTION HISTORY",rows,uid),ephemeral=True)

class AdminPanelView(LegacyAdminPanelView):
    def __init__(self):
        super().__init__()
        self.add_item(discord.ui.Button(label="総通貨量",style=discord.ButtonStyle.primary,custom_id="final_totals",row=3))
        self.add_item(discord.ui.Button(label="交換設定",style=discord.ButtonStyle.primary,custom_id="final_settings",row=3))
        self.add_item(discord.ui.Button(label="このchを送金審査chに設定",style=discord.ButtonStyle.secondary,custom_id="final_review_ch",row=4))
        self.children[-3].callback=self.show_totals;self.children[-2].callback=self.settings;self.children[-1].callback=self.review_ch
    async def show_totals(self,i):
        x=await totals();await i.response.send_message(f"💰 **PAL総量 {x['PAL']:,} PAL**\n🎰 **CHIP総量 {x['CHIP']:,} CHIP**",ephemeral=True)
    async def settings(self,i):await i.response.send_modal(SettingsModal())
    async def review_ch(self,i):
        await set_setting("transfer_review_channel_id",str(i.channel_id));await i.response.send_message("✅ このチャンネルを送金審査chに設定しました。",ephemeral=True)
