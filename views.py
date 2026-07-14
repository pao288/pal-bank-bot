import discord

from accounts import ensure_user_accounts, get_user_balances


class BankPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="💰 残高確認",
        style=discord.ButtonStyle.primary,
        custom_id="bank_check_balance",
    )
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)

        await ensure_user_accounts(user_id)
        balances = await get_user_balances(user_id)

        embed = discord.Embed(title="🏦 あなたの口座")
        embed.add_field(name="💰 PAL", value=f"{balances['PAL']:,} PAL", inline=False)
        embed.add_field(name="🎰 CHIP", value=f"{balances['CHIP']:,} CHIP", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)
