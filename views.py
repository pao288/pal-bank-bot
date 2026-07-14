import random
import uuid

import discord

from accounts import ensure_user_accounts, get_user_account_id, get_user_balances
from db import get_pool
from transactions import (
    AlreadyProcessedError,
    InsufficientBalanceError,
    get_all_history,
    get_history,
    transfer,
)


def money(value: int, currency: str) -> str:
    return f"{value:,} {currency}"


def history_embed(title: str, rows: list, viewer_id: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title)
    if not rows:
        embed.description = "取引履歴はありません。"
        return embed

    lines = []
    for row in rows[:100]:
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
            f"{row['transaction_type']} / {row['amount']:,} {row['currency']}"
        )

    # Discord embed description limit対策
    embed.description = "\n".join(lines[:40])
    embed.set_footer(text=f"最新 {min(len(rows), 100)} 件")
    return embed


class TransferAmountModal(discord.ui.Modal, title="💸 送金"):
    amount = discord.ui.TextInput(
        label="送金額",
        placeholder="例: 10000",
        max_length=18,
    )

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

        await ensure_user_accounts(sender_id)
        await ensure_user_accounts(target_id)
        from_id = await get_user_account_id(sender_id, "PAL")
        to_id = await get_user_account_id(target_id, "PAL")

        key = f"USER_TRANSFER:{interaction.id}:{uuid.uuid4()}"
        try:
            await transfer(
                key,
                "USER_TRANSFER",
                "PAL",
                from_id,
                to_id,
                amount,
                external_bot="PAL_BANK",
                external_reference_id=str(interaction.id),
                metadata={"sender_id": sender_id, "target_id": target_id},
            )
        except InsufficientBalanceError:
            await interaction.response.send_message("PAL残高が足りません。", ephemeral=True)
            return

        embed = discord.Embed(title="✅ 送金完了")
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
    total_amount = discord.ui.TextInput(
        label="合計PAL",
        placeholder="例: 10000",
        max_length=18,
    )
    max_claims = discord.ui.TextInput(
        label="受取人数",
        placeholder="例: 10",
        max_length=3,
    )

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
        await ensure_user_accounts(creator_id)
        source_id = await get_user_account_id(creator_id, "PAL")

        # ランダム配分。全枠1PAL以上、合計はtotalと一致。
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
                        INSERT INTO bank.pal_envelopes (
                            creator_id, total_amount, max_claims, status, source_channel_id
                        )
                        VALUES ($1,$2,$3,'ACTIVE',$4)
                        RETURNING envelope_id
                        """,
                        creator_id,
                        total,
                        claims,
                        str(interaction.channel_id),
                    )

                    await conn.execute(
                        """
                        UPDATE bank.accounts
                        SET balance = balance - $1, updated_at = now()
                        WHERE account_id = $2
                        """,
                        total,
                        source_id,
                    )

                    await conn.execute(
                        """
                        INSERT INTO bank.transactions (
                            idempotency_key, transaction_type, currency,
                            from_account_id, to_account_id, amount,
                            external_bot, external_reference_id, status, metadata,
                            completed_at
                        )
                        VALUES ($1,'PAL_ENVELOPE_CREATE','PAL',$2,NULL,$3,
                                'PAL_BANK',$4,'COMPLETED',
                                jsonb_build_object('envelope_id',$5::bigint),now())
                        """,
                        key,
                        source_id,
                        total,
                        str(envelope_id),
                        envelope_id,
                    )

                    await conn.executemany(
                        """
                        INSERT INTO bank.pal_envelope_slots (envelope_id, amount, slot_order)
                        VALUES ($1,$2,$3)
                        """,
                        [(envelope_id, amount, i + 1) for i, amount in enumerate(slots)],
                    )
        except InsufficientBalanceError:
            await interaction.response.send_message("PAL残高が足りません。", ephemeral=True)
            return

        embed = discord.Embed(title="🧧 PALポチ袋")
        embed.description = (
            f"{interaction.user.mention} がポチ袋を作成しました！\n"
            f"**合計 {total:,} PAL / {claims}人**\n"
            "早い者勝ちで1人1回受け取れます。"
        )
        view = EnvelopeClaimView(envelope_id)
        await interaction.response.send_message("ポチ袋を作成しました。", ephemeral=True)
        message = await interaction.channel.send(embed=embed, view=view)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE bank.pal_envelopes
                SET source_message_id=$1
                WHERE envelope_id=$2
                """,
                str(message.id),
                envelope_id,
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
        await ensure_user_accounts(user_id)
        to_id = await get_user_account_id(user_id, "PAL")
        pool = get_pool()

        async with pool.acquire() as conn:
            async with conn.transaction():
                envelope = await conn.fetchrow(
                    """
                    SELECT * FROM bank.pal_envelopes
                    WHERE envelope_id=$1
                    FOR UPDATE
                    """,
                    self.envelope_id,
                )
                if envelope is None or envelope["status"] != "ACTIVE":
                    await interaction.response.send_message("このポチ袋は終了しています。", ephemeral=True)
                    return

                claimed = await conn.fetchval(
                    """
                    SELECT 1 FROM bank.pal_envelope_claims
                    WHERE envelope_id=$1 AND user_id=$2
                    """,
                    self.envelope_id,
                    user_id,
                )
                if claimed:
                    await interaction.response.send_message("このポチ袋は受取済みです。", ephemeral=True)
                    return

                slot = await conn.fetchrow(
                    """
                    SELECT slot_id, amount
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
                        "UPDATE bank.pal_envelopes SET status='COMPLETED', completed_at=now() WHERE envelope_id=$1",
                        self.envelope_id,
                    )
                    await interaction.response.send_message("このポチ袋は終了しています。", ephemeral=True)
                    return

                await conn.execute(
                    """
                    UPDATE bank.pal_envelope_slots
                    SET claimed_by=$1, claimed_at=now()
                    WHERE slot_id=$2
                    """,
                    user_id,
                    slot["slot_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO bank.pal_envelope_claims
                    (envelope_id,user_id,slot_id,amount)
                    VALUES ($1,$2,$3,$4)
                    """,
                    self.envelope_id,
                    user_id,
                    slot["slot_id"],
                    slot["amount"],
                )
                await conn.execute(
                    """
                    UPDATE bank.accounts
                    SET balance=balance+$1, updated_at=now()
                    WHERE account_id=$2
                    """,
                    slot["amount"],
                    to_id,
                )
                await conn.execute(
                    """
                    INSERT INTO bank.transactions (
                        idempotency_key, transaction_type, currency,
                        from_account_id, to_account_id, amount,
                        external_bot, external_reference_id, status, metadata,
                        completed_at
                    )
                    VALUES ($1,'PAL_ENVELOPE_CLAIM','PAL',NULL,$2,$3,
                            'PAL_BANK',$4,'COMPLETED',
                            jsonb_build_object('envelope_id',$5::bigint),now())
                    """,
                    f"ENVELOPE_CLAIM:{self.envelope_id}:{user_id}",
                    to_id,
                    slot["amount"],
                    str(self.envelope_id),
                    self.envelope_id,
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
                        """
                        UPDATE bank.pal_envelopes
                        SET status='COMPLETED', completed_at=now()
                        WHERE envelope_id=$1
                        """,
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


class BankPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 残高確認", style=discord.ButtonStyle.primary, custom_id="bank_check_balance", row=0)
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        balances = await get_user_balances(str(interaction.user.id))
        embed = discord.Embed(title="🏦 あなたの口座")
        embed.add_field(name="💰 PAL", value=money(balances["PAL"], "PAL"), inline=False)
        embed.add_field(name="🎰 CHIP", value=money(balances["CHIP"], "CHIP"), inline=False)
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
            embed=history_embed("📖 あなたの取引履歴", rows, user_id),
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

        target_id = str(self.target.id)
        account_id = await get_user_account_id(target_id, self.currency)
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
                metadata={"admin_id": str(interaction.user.id), "target_id": target_id},
            )
        except InsufficientBalanceError:
            await interaction.response.send_message("対象ユーザーの残高が足りません。", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ {self.target.mention} / {amount:,} {self.currency} / "
            f"{'付与' if self.action == 'GRANT' else '回収'} 完了",
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
            embed = discord.Embed(title=f"🏦 {target.display_name} の残高")
            embed.add_field(name="PAL", value=money(balances["PAL"], "PAL"), inline=False)
            embed.add_field(name="CHIP", value=money(balances["CHIP"], "CHIP"), inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.send_modal(
            AdminAmountModal(target, self.currency, self.action)
        )


class AdminTargetView(discord.ui.View):
    def __init__(self, mode: str, currency: str | None = None, action: str | None = None):
        super().__init__(timeout=120)
        self.add_item(AdminTargetSelect(mode, currency, action))


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

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
            embed=history_embed("📖 BANK全体 取引履歴", rows),
            ephemeral=True,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("管理者専用パネルです。", ephemeral=True)
            return False
        return True
