import discord

from accounts import ensure_user_accounts, get_user_balances


class BankPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💰 残高確認",
        style=discord.ButtonStyle.primary,
        custom_id="bank_check_balance",
        row=0,
    )
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)

        await ensure_user_accounts(user_id)
        balances = await get_user_balances(user_id)

        embed = discord.Embed(title="🏦 あなたの口座")
        embed.add_field(name="💰 PAL", value=f"{balances['PAL']:,} PAL", inline=False)
        embed.add_field(name="🎰 CHIP", value=f"{balances['CHIP']:,} CHIP", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="💸 送金",
        style=discord.ButtonStyle.secondary,
        custom_id="bank_transfer",
        row=0,
    )
    async def transfer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "💸 送金機能は準備中です。次のアップデートで実装します。",
            ephemeral=True,
        )

    @discord.ui.button(
        label="🧧 ポチ袋作成",
        style=discord.ButtonStyle.secondary,
        custom_id="bank_envelope_create",
        row=0,
    )
    async def envelope_create_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "🧧 PALポチ袋機能は準備中です。次のアップデートで実装します。",
            ephemeral=True,
        )

    @discord.ui.button(
        label="📖 取引履歴",
        style=discord.ButtonStyle.secondary,
        custom_id="bank_history",
        row=1,
    )
    async def history_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "📖 取引履歴機能は準備中です。次のアップデートで実装します。",
            ephemeral=True,
        )
