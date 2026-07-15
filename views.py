import random
import uuid
import io

import discord

from accounts import ensure_user_accounts, get_user_account_id, get_user_balances
from db import get_pool, get_setting, set_setting
from transactions import InsufficientBalanceError, get_all_history, get_history, transfer
from bank_services import exchange, totals
from bank_advanced import (
    add_notification, atomic_exchange, csv_export_bytes, duplicate_pending,
    get_notifications, maintenance_enabled, profile, reverse_transaction,
    search_transactions, set_maintenance, statistics, transfer_warning,
)


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


class EnvelopeDraft:
    def __init__(self, creator_id: str, total: int, claims: int):
        self.creator_id = creator_id
        self.total = total
        self.claims = claims
        self.distribution = None


class EnvelopeModal(discord.ui.Modal, title="🧧 PALポチ袋作成"):
    total_amount = discord.ui.TextInput(label="合計PAL", placeholder="例: 10000", max_length=18)
    max_claims = discord.ui.TextInput(label="受取人数", placeholder="1〜100", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        if await maintenance_enabled():
            await interaction.response.send_message("BANKはメンテナンスモード中です。", ephemeral=True)
            return
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

        balances = await get_user_balances(str(interaction.user.id))
        if balances["PAL"] < total:
            await interaction.response.send_message("PAL残高が足りません。", ephemeral=True)
            return

        draft = EnvelopeDraft(str(interaction.user.id), total, claims)
        embed = bank_embed("🧧 配布方式を選択", color=PAL_GOLD)
        embed.description = (
            f"💰 合計 **{total:,} PAL**\n"
            f"👥 **{claims}人**\n\n"
            "🎲 **ランダム**：受取額をランダム配分\n"
            "⚖️ **均等**：全員へ均等配分"
        )
        await interaction.response.send_message(
            embed=embed,
            view=EnvelopeDistributionView(draft),
            ephemeral=True,
        )


class EnvelopeDistributionView(discord.ui.View):
    def __init__(self, draft: EnvelopeDraft):
        super().__init__(timeout=180)
        self.draft = draft

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.draft.creator_id:
            await interaction.response.send_message("作成者専用です。", ephemeral=True)
            return False
        return True

    async def show_channel_select(self, interaction: discord.Interaction, distribution: str):
        self.draft.distribution = distribution
        label = "🎲 ランダム" if distribution == "RANDOM" else "⚖️ 均等"
        await interaction.response.edit_message(
            content=f"{label}を選択しました。送信先のテキストチャンネルを選んでください。",
            embed=None,
            view=EnvelopeChannelView(self.draft),
        )

    @discord.ui.button(label="🎲 ランダム", style=discord.ButtonStyle.primary)
    async def random_distribution(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_channel_select(interaction, "RANDOM")

    @discord.ui.button(label="⚖️ 均等", style=discord.ButtonStyle.secondary)
    async def equal_distribution(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_channel_select(interaction, "EQUAL")


class EnvelopeChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, draft: EnvelopeDraft):
        super().__init__(
            placeholder="ポチ袋を送信するテキストチャンネルを選択",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.draft = draft

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.draft.creator_id:
            await interaction.response.send_message("作成者専用です。", ephemeral=True)
            return

        target_channel = self.values[0]
        creator_id = self.draft.creator_id
        total = self.draft.total
        claims = self.draft.claims

        if self.draft.distribution == "RANDOM":
            cuts = sorted(random.sample(range(1, total), claims - 1)) if claims > 1 else []
            points = [0] + cuts + [total]
            slots = [points[i + 1] - points[i] for i in range(claims)]
            random.shuffle(slots)
            distribution_text = "🎲 ランダム配布"
        else:
            base, remainder = divmod(total, claims)
            slots = [base + (1 if i < remainder else 0) for i in range(claims)]
            random.shuffle(slots)
            distribution_text = "⚖️ 均等配布"

        source_id = await get_user_account_id(creator_id, "PAL")
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
                        creator_id, total, claims, str(target_channel.id),
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
                                jsonb_build_object(
                                    'envelope_id',$5::bigint,
                                    'distribution',$6::text,
                                    'target_channel_id',$7::text
                                ),now())
                        """,
                        key, source_id, total, str(envelope_id), envelope_id,
                        self.draft.distribution, str(target_channel.id),
                    )
                    await conn.executemany(
                        """
                        INSERT INTO bank.pal_envelope_slots(envelope_id,amount,slot_order)
                        VALUES ($1,$2,$3)
                        """,
                        [(envelope_id, value, i + 1) for i, value in enumerate(slots)],
                    )
        except InsufficientBalanceError:
            await interaction.response.edit_message(
                content="PAL残高が足りません。",
                embed=None,
                view=None,
            )
            return

        embed = bank_embed("🧧 PAL ENVELOPE", color=PAL_GOLD)
        embed.description = (
            f"<@{creator_id}> からPALポチ袋！\n\n"
            f"💰 **合計 {total:,} PAL**\n"
            f"👥 **{claims}人限定**\n"
            f"🎁 **{distribution_text}**\n\n"
            "下のボタンから受け取ってください。"
        )

        try:
            message = await target_channel.send(
                embed=embed,
                view=EnvelopeClaimView(envelope_id),
            )
        except (discord.Forbidden, discord.HTTPException):
            # Return reserved PAL if posting failed.
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE bank.accounts SET balance=balance+$1,updated_at=now() WHERE account_id=$2",
                        total, source_id,
                    )
                    await conn.execute(
                        "UPDATE bank.pal_envelopes SET status='CANCELLED' WHERE envelope_id=$1",
                        envelope_id,
                    )
                    await conn.execute(
                        """
                        UPDATE bank.transactions
                        SET status='REFUNDED'
                        WHERE idempotency_key=$1
                        """,
                        key,
                    )
            await interaction.response.edit_message(
                content="そのテキストチャンネルへ投稿できませんでした。別のチャンネルを選んで作成してください。",
                embed=None,
                view=None,
            )
            return

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE bank.pal_envelopes
                SET source_message_id=$1
                WHERE envelope_id=$2
                """,
                str(message.id), envelope_id,
            )

        await interaction.response.edit_message(
            content=f"✅ {target_channel.mention} にポチ袋を送信しました。",
            embed=None,
            view=None,
        )


class EnvelopeChannelView(discord.ui.View):
    def __init__(self, draft: EnvelopeDraft):
        super().__init__(timeout=180)
        self.add_item(EnvelopeChannelSelect(draft))


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
    reason = discord.ui.TextInput(label="理由", placeholder="例: イベント景品 / 補填 / SHOP返金", max_length=100)

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
                metadata={"admin_id": str(interaction.user.id), "target_id": str(self.target.id), "reason": str(self.reason), "public_tx_id": f"ADMIN-{interaction.id}"},
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
        await add_notification(q["requester_id"],"TRANSFER_APPROVED","送金が許可されました",f"{q['amount']:,} PAL の送金申請 #{self.request_id:06d} が許可されました。")
        await add_notification(q["recipient_id"],"TRANSFER_RECEIVED","PALを受け取りました",f"{q['amount']:,} PAL を受け取りました。取引 #{self.request_id:06d}")
        await i.response.edit_message(content=f"✅ 許可済み｜審査者 {i.user.mention}",view=None)
    async def reject(self,i):
        pool=get_pool()
        async with pool.acquire() as c:
            result=await c.execute("UPDATE bank.transfer_requests SET status='REJECTED',reviewed_by=$1,reviewed_at=now() WHERE request_id=$2 AND status='PENDING'",str(i.user.id),self.request_id)
        if result.endswith("0"): await i.response.send_message("審査済みです。",ephemeral=True);return
        pool=get_pool()
        async with pool.acquire() as c:
            q=await c.fetchrow("SELECT requester_id,amount FROM bank.transfer_requests WHERE request_id=$1",self.request_id)
        if q: await add_notification(q["requester_id"],"TRANSFER_REJECTED","送金が却下されました",f"{q['amount']:,} PAL の送金申請 #{self.request_id:06d} が却下されました。審査中PALは利用可能に戻りました。")
        await i.response.edit_message(content=f"❌ 却下済み｜審査者 {i.user.mention}",embed=None,view=None)

class RequestAmountModal(discord.ui.Modal,title="💸 PAL送金申請"):
    amount=discord.ui.TextInput(label="送金額",placeholder="例: 10000",max_length=18)
    def __init__(self,target):super().__init__();self.target=target
    async def on_submit(self,i):
        try:
            amount=int(str(self.amount).replace(",",""));assert amount>0
        except: await i.response.send_message("1以上の数字を入力してください。",ephemeral=True);return
        if await maintenance_enabled():
            await i.response.send_message("BANKはメンテナンスモード中です。",ephemeral=True);return
        p=await profile(str(i.user.id))
        if p["available_pal"]<amount:
            await i.response.send_message(f"利用可能PALが足りません。利用可能: {p['available_pal']:,} PAL",ephemeral=True);return
        duplicate=await duplicate_pending(str(i.user.id),str(self.target.id),amount)
        if duplicate:
            await i.response.send_message(f"同じ内容の審査中申請があります。`#{duplicate['request_id']:06d}`",ephemeral=True);return
        warning=await transfer_warning(str(i.user.id),amount)
        ch=await get_setting("transfer_review_channel_id")
        if not ch: await i.response.send_message("送金審査チャンネルが未設定です。",ephemeral=True);return
        pool=get_pool()
        async with pool.acquire() as c:
            rid=await c.fetchval("INSERT INTO bank.transfer_requests(requester_id,recipient_id,amount,status,warning_text) VALUES($1,$2,$3,'PENDING',$4) RETURNING request_id",str(i.user.id),str(self.target.id),amount,warning)
        channel=i.client.get_channel(int(ch)) or await i.client.fetch_channel(int(ch))
        e=bank_embed(f"💸 PAL送金審査｜#{rid:06d}",color=PAL_GOLD)
        e.description=f"申請者: {i.user.mention}\n送金先: {self.target.mention}\n金額: **{amount:,} PAL**\n利用可能残高: {p['available_pal']:,} PAL" + (f"\n\n⚠️ **警告**\n{warning}" if warning else "")
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
            a=int(str(self.amount).replace(",",""));x,xc,y,yc,txid=await atomic_exchange(str(i.user.id),self.d,a)
            await i.response.send_message(f"✅ **{x:,} {xc} → {y:,} {yc}**\n取引ID: `{txid}`",ephemeral=True)
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


class TransactionSearchModal(discord.ui.Modal, title="🔎 取引検索"):
    user_id = discord.ui.TextInput(label="ユーザーID（空欄可）", required=False, max_length=25)
    currency = discord.ui.TextInput(label="通貨 PAL / CHIP（空欄可）", required=False, max_length=4)
    source = discord.ui.TextInput(label="種類 SHOP / CASINO / VOICE / ADMIN 等", required=False, max_length=30)

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(self.user_id).strip() or None
        cur = str(self.currency).strip().upper() or None
        src = str(self.source).strip().upper() or None
        rows = await search_transactions(uid, cur, src, 25)
        lines = []
        for r in rows:
            meta = r["metadata"] or {}
            txid = meta.get("public_tx_id") or f"DB-{r['bank_transaction_id']}"
            reason = meta.get("reason") or "-"
            lines.append(f"`{txid}` {r['transaction_type']}｜{r['amount']:,} {r['currency']}｜理由:{reason}")
        e = bank_embed("🔎 TRANSACTION SEARCH", color=PAL_DARK)
        e.description = "\n".join(lines[:25]) if lines else "該当取引なし"
        await interaction.response.send_message(embed=e, ephemeral=True)


class ReversalModal(discord.ui.Modal, title="↩️ 取引取消・返金"):
    transaction_id = discord.ui.TextInput(label="DB取引ID", placeholder="例: 123", max_length=20)
    reason = discord.ui.TextInput(label="取消・返金理由", placeholder="例: 誤付与の取消", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            txid = int(str(self.transaction_id).strip())
            code = await reverse_transaction(txid, str(interaction.user.id), str(self.reason))
            await interaction.response.send_message(f"✅ 逆取引を作成しました。取引ID: `{code}`", ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class UltimateBankPanelView(BankPanelView):
    @discord.ui.button(label="👤 BANKプロフィール", style=discord.ButtonStyle.primary, custom_id="ultimate_profile", row=2)
    async def bank_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await profile(str(interaction.user.id))
        e = bank_embed("💳 PAL BANK PROFILE", color=PAL_GOLD)
        e.add_field(name="💰 PAL残高", value=f"**{p['PAL']:,} PAL**", inline=True)
        e.add_field(name="🎰 CHIP残高", value=f"**{p['CHIP']:,} CHIP**", inline=True)
        e.add_field(name="🔓 利用可能PAL", value=f"**{p['available_pal']:,} PAL**", inline=True)
        e.add_field(name="⏳ 審査中PAL", value=f"**{p['pending_pal']:,} PAL**", inline=True)
        e.add_field(name="🏆 総資産", value=f"**{p['asset_pal']:,} PAL換算**", inline=True)
        e.add_field(name="📊 資産順位", value=f"**#{p['rank']}**", inline=True)
        e.add_field(name="🔔 未読通知", value=f"**{p['unread']}件**", inline=True)
        if p["created_at"]:
            e.set_footer(text=f"口座開設: {p['created_at']:%Y-%m-%d} • 1 CHIP = {p['rate']:,} PAL")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🔔 通知", style=discord.ButtonStyle.secondary, custom_id="ultimate_notifications", row=2)
    async def notifications(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await get_notifications(str(interaction.user.id), 10)
        e = bank_embed("🔔 BANK NOTIFICATIONS", color=PAL_DARK)
        e.description = "\n\n".join(
            f"{'🔵' if not r['is_read'] else '⚪'} **{r['title']}**\n{r['body']}"
            for r in rows
        ) if rows else "通知はありません。"
        await interaction.response.send_message(embed=e, ephemeral=True)


class UltimateAdminPanelView(AdminPanelView):
    def __init__(self):
        super().__init__()
        items = [
            ("通貨統計", discord.ButtonStyle.primary, "ultimate_stats", 3, self.show_stats),
            ("取引検索", discord.ButtonStyle.secondary, "ultimate_search", 3, self.search),
            ("取消・返金", discord.ButtonStyle.danger, "ultimate_reversal", 3, self.reversal),
            ("メンテON/OFF", discord.ButtonStyle.danger, "ultimate_maintenance", 4, self.maintenance),
            ("このchを移動ログchに設定", discord.ButtonStyle.secondary, "ultimate_log_ch", 4, self.log_channel),
            ("このchをBANK状態chに設定", discord.ButtonStyle.secondary, "ultimate_status_ch", 4, self.status_channel),
            ("CSV出力", discord.ButtonStyle.secondary, "ultimate_csv", 4, self.csv_export),
        ]
        for label, style, cid, row, callback in items:
            b = discord.ui.Button(label=label, style=style, custom_id=cid, row=row)
            b.callback = callback
            self.add_item(b)

    async def show_stats(self, interaction):
        s = await statistics()
        e = bank_embed("📈 PAL BANK STATISTICS", color=PAL_DARK)
        e.add_field(name="口座ユーザー", value=f"{s['users']:,}人")
        e.add_field(name="完了取引", value=f"{s['completed']:,}件")
        e.add_field(name="24時間取引", value=f"{s['tx24']:,}件")
        e.add_field(name="PAL移動総量", value=f"{s['moved_pal']:,} PAL")
        e.add_field(name="CHIP移動総量", value=f"{s['moved_chip']:,} CHIP")
        e.add_field(name="送金審査中", value=f"{s['req_pending']:,}件")
        e.add_field(name="送金許可", value=f"{s['req_approved']:,}件")
        e.add_field(name="送金却下", value=f"{s['req_rejected']:,}件")
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def search(self, interaction):
        await interaction.response.send_modal(TransactionSearchModal())

    async def reversal(self, interaction):
        await interaction.response.send_modal(ReversalModal())

    async def maintenance(self, interaction):
        now = await maintenance_enabled()
        await set_maintenance(not now)
        await interaction.response.send_message(
            f"{'🔴 メンテナンスモード ON' if not now else '🟢 メンテナンスモード OFF'}",
            ephemeral=True,
        )

    async def log_channel(self, interaction):
        await set_setting("movement_log_channel_id", str(interaction.channel_id))
        await interaction.response.send_message("✅ このチャンネルを通貨移動ログchに設定しました。", ephemeral=True)

    async def status_channel(self, interaction):
        await set_setting("bank_status_channel_id", str(interaction.channel_id))
        await interaction.response.send_message("✅ このチャンネルをBANKステータスchに設定しました。", ephemeral=True)

    async def csv_export(self, interaction):
        data = await csv_export_bytes()
        await interaction.response.send_message(
            "直近最大5,000件の取引CSVです。",
            file=discord.File(io.BytesIO(data), filename="pal_bank_transactions.csv"),
            ephemeral=True,
        )


# ===== CLEAN FINAL UI =====

class CurrencyActionSelect(discord.ui.Select):
    def __init__(self, currency: str):
        self.currency = currency
        super().__init__(
            placeholder=f"{currency}の操作を選択",
            options=[
                discord.SelectOption(label=f"{currency}付与", value="GRANT", emoji="➕"),
                discord.SelectOption(label=f"{currency}回収", value="TAKE", emoji="➖"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"{self.currency}の対象ユーザーを選択してください。",
            view=AdminUserView(self.currency, self.values[0]),
            ephemeral=True,
        )


class CurrencyActionView(discord.ui.View):
    def __init__(self, currency: str):
        super().__init__(timeout=120)
        self.add_item(CurrencyActionSelect(currency))


class TransactionActionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="取引操作を選択",
            options=[
                discord.SelectOption(label="取引確認", value="HISTORY", emoji="📖"),
                discord.SelectOption(label="取引検索", value="SEARCH", emoji="🔎"),
                discord.SelectOption(label="取引返金・取消", value="REVERSAL", emoji="↩️"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        action = self.values[0]
        if action == "HISTORY":
            rows = await get_all_history(100)
            await interaction.response.send_message(
                embed=history_embed("📖 BANK TRANSACTION HISTORY", rows),
                ephemeral=True,
            )
        elif action == "SEARCH":
            await interaction.response.send_modal(TransactionSearchModal())
        else:
            await interaction.response.send_modal(ReversalModal())


class TransactionActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(TransactionActionSelect())


class BankPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="👤 BANKプロフィール", style=discord.ButtonStyle.primary, custom_id="clean_profile", row=0)
    async def bank_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = await profile(str(interaction.user.id))
        e = bank_embed("💳 PAL BANK PROFILE", color=PAL_GOLD)
        e.add_field(name="💰 PAL残高", value=f"**{p['PAL']:,} PAL**", inline=True)
        e.add_field(name="🎰 CHIP残高", value=f"**{p['CHIP']:,} CHIP**", inline=True)
        e.add_field(name="🔓 利用可能PAL", value=f"**{p['available_pal']:,} PAL**", inline=True)
        e.add_field(name="⏳ 審査中PAL", value=f"**{p['pending_pal']:,} PAL**", inline=True)
        e.add_field(name="🏆 総資産", value=f"**{p['asset_pal']:,} PAL換算**", inline=True)
        e.add_field(name="📊 資産順位", value=f"**#{p['rank']}**", inline=True)
        e.add_field(name="🔔 未読通知", value=f"**{p['unread']}件**", inline=True)
        if p["created_at"]:
            e.set_footer(text=f"口座開設: {p['created_at']:%Y-%m-%d} • 1 CHIP = {p['rate']:,} PAL")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="💸 送金申請", style=discord.ButtonStyle.secondary, custom_id="clean_transfer", row=0)
    async def transfer_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("送金相手を選択してください。", view=RequestUserView(), ephemeral=True)

    @discord.ui.button(label="🔄 通貨交換", style=discord.ButtonStyle.primary, custom_id="clean_exchange", row=0)
    async def exchange_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rate = await get_setting("chip_rate_pal", "100")
        fee = await get_setting("exchange_fee_percent", "0")
        minimum = await get_setting("exchange_min_pal", "1000")
        await interaction.response.send_message(
            f"**1 CHIP = {int(rate):,} PAL｜手数料 {fee}%｜最低 {int(minimum):,} PAL**",
            view=ExchangeView2(),
            ephemeral=True,
        )

    @discord.ui.button(label="📖 取引履歴", style=discord.ButtonStyle.secondary, custom_id="clean_history", row=1)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        rows = await get_history(uid, 100)
        await interaction.response.send_message(
            embed=history_embed("📖 TRANSACTION HISTORY", rows, uid),
            ephemeral=True,
        )

    @discord.ui.button(label="🔔 通知", style=discord.ButtonStyle.secondary, custom_id="clean_notifications", row=1)
    async def notifications(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await get_notifications(str(interaction.user.id), 10)
        e = bank_embed("🔔 BANK NOTIFICATIONS", color=PAL_DARK)
        e.description = "\n\n".join(
            f"{'🔵' if not r['is_read'] else '⚪'} **{r['title']}**\n{r['body']}" for r in rows
        ) if rows else "通知はありません。"
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🧧 ポチ袋", style=discord.ButtonStyle.secondary, custom_id="clean_envelope", row=1)
    async def envelope(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EnvelopeModal())


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("管理者専用です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💰 PAL付与・回収", style=discord.ButtonStyle.primary, custom_id="clean_admin_pal", row=0)
    async def pal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("PALの操作を選択してください。", view=CurrencyActionView("PAL"), ephemeral=True)

    @discord.ui.button(label="🎰 CHIP付与・回収", style=discord.ButtonStyle.primary, custom_id="clean_admin_chip", row=0)
    async def chip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("CHIPの操作を選択してください。", view=CurrencyActionView("CHIP"), ephemeral=True)

    @discord.ui.button(label="👤 ユーザー残高確認", style=discord.ButtonStyle.secondary, custom_id="clean_admin_balance", row=0)
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("確認するユーザーを選択してください。", view=AdminUserView("PAL", "BALANCE"), ephemeral=True)

    @discord.ui.button(label="🏦 総通貨", style=discord.ButtonStyle.secondary, custom_id="clean_admin_totals", row=1)
    async def total_currency(self, interaction: discord.Interaction, button: discord.ui.Button):
        x = await totals()
        await interaction.response.send_message(
            f"💰 **PAL総量 {x['PAL']:,} PAL**\n🎰 **CHIP総量 {x['CHIP']:,} CHIP**",
            ephemeral=True,
        )

    @discord.ui.button(label="🔄 交換設定", style=discord.ButtonStyle.secondary, custom_id="clean_admin_exchange", row=1)
    async def exchange_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SettingsModal())

    @discord.ui.button(label="📖 取引管理", style=discord.ButtonStyle.secondary, custom_id="clean_admin_transactions", row=1)
    async def transactions(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("取引操作を選択してください。", view=TransactionActionView(), ephemeral=True)


async def _toggle_managed_channel(interaction: discord.Interaction, setting_key: str, name: str, topic: str):
    guild = interaction.guild
    existing_id = await get_setting(setting_key, "0")

    if existing_id and existing_id != "0":
        channel = guild.get_channel(int(existing_id))
        if channel is not None:
            await set_setting(setting_key, "0")
            await interaction.response.send_message(f"🗑️ {channel.mention} を削除します。", ephemeral=True)
            await channel.delete(reason=f"PAL BANK managed channel toggle by {interaction.user}")
            return
        await set_setting(setting_key, "0")

    channel = await guild.create_text_channel(
        name=name,
        topic=topic,
        reason=f"PAL BANK managed channel created by {interaction.user}",
    )
    await set_setting(setting_key, str(channel.id))
    await interaction.response.send_message(f"✅ {channel.mention} を作成しました。", ephemeral=True)


class BankSetupPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("管理者専用です。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="💸 送金審査チャンネル ON / OFF", style=discord.ButtonStyle.primary, custom_id="setup_review_channel", row=0)
    async def review_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_managed_channel(
            interaction, "transfer_review_channel_id",
            "pal-送金審査", "PAL BANK｜送金申請の許可・却下を行うチャンネル",
        )

    @discord.ui.button(label="📜 通貨移動ログ ON / OFF", style=discord.ButtonStyle.secondary, custom_id="setup_log_channel", row=1)
    async def movement_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_managed_channel(
            interaction, "movement_log_channel_id",
            "pal-通貨移動ログ", "PAL BANK｜SEND・SHOP・CASINO・VOICE・ADMIN・BANK通貨移動ログ",
        )

    @discord.ui.button(label="🟢 BANKステータス ON / OFF", style=discord.ButtonStyle.secondary, custom_id="setup_status_channel", row=2)
    async def bank_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _toggle_managed_channel(
            interaction, "bank_status_channel_id",
            "pal-bank-status", "PAL BANK｜稼働状況・交換レート・手数料・口座数・24時間取引",
        )
