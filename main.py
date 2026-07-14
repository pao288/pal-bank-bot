import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"PAL BANK 起動完了: {bot.user}")

bot.run(os.getenv("DISCORD_TOKEN"))
