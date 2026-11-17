import os
import asyncio
from typing import Optional

import discord
from discord.ext import commands
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# Config
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))  # Set this to your tickets category ID
SUPPORT_ROLE_ID = int(os.getenv("SUPPORT_ROLE_ID", "0"))  # Optional staff role ID

if not DISCORD_TOKEN or not XAI_API_KEY:
    raise ValueError("Missing DISCORD_TOKEN or XAI_API_KEY in .env")

# AI Client (xAI / Grok)
ai_client = AsyncOpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1",
)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket_btn", emoji="🎟")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            return await interaction.followup.send("This can only be used in a server.", ephemeral=True)

        # Check if user already has an open ticket
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not category or not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send("Ticket category not configured properly. Ask an admin to set TICKET_CATEGORY_ID.", ephemeral=True)

        existing = discord.utils.get(category.channels, name=f"ticket-{interaction.user.name.lower().replace(' ', '-')}")
        if existing:
            return await interaction.followup.send(f"You already have an open ticket: {existing.mention}", ephemeral=True)

        # Create private channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
        }
        if SUPPORT_ROLE_ID:
            role = guild.get_role(SUPPORT_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await category.create_text_channel(
            name=f"ticket-{interaction.user.name.lower().replace(' ', '-')}",
            overwrites=overwrites,
            topic=f"Support ticket for {interaction.user} (ID: {interaction.user.id})",
            reason="New support ticket created"
        )

        embed = discord.Embed(
            title="🎟 Support Ticket",
            description=f"Hello {interaction.user.mention}!\n\nDescribe your issue below and the AI assistant will help you.\nStaff can also respond here.\n\nClick the button below to close this ticket when you're done.",
            color=discord.Color.green()
        )
        embed.set_footer(text="Powered by Grok AI")

        view = TicketControls(channel.id)
        await channel.send(embed=embed, view=view)
        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)


class TicketControls(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel.id != self.channel_id:
            return

        await interaction.response.send_message("Closing ticket in 5 seconds...", ephemeral=False)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            await interaction.followup.send("Failed to delete channel. Please delete it manually.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # Register persistent views
    bot.add_view(TicketPanel())
    # Note: TicketControls needs to be re-added if you want buttons to work after restart.
    # For simplicity, new tickets get fresh views. For full persistence of close buttons,
    # store channel IDs and re-register on startup (advanced).
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.tree.command(name="setup", description="Post the ticket creation panel")
@commands.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Need Help?",
        description="Click the button below to open a private support ticket.\nAn AI assistant will help answer your questions!",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Powered by Grok")
    await interaction.response.send_message(embed=embed, view=TicketPanel())


async def get_ai_response(messages: list) -> str:
    try:
        response = await ai_client.chat.completions.create(
            model="grok-4",  # or "grok-3", "grok-4.7" etc. Check available models
            messages=messages,
            temperature=0.7,
            max_tokens=800,
        )
        return response.choices[0].message.content or "Sorry, I couldn't generate a response."
    except Exception as e:
        print(f"AI Error: {e}")
        return "Sorry, the AI is temporarily unavailable. A staff member will help you soon."


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Only respond in ticket channels
    if not message.channel.name.startswith("ticket-"):
        await bot.process_commands(message)
        return

    # Ignore if it's a staff-only message or commands
    if message.content.startswith("!") or message.content.startswith("/"):
        await bot.process_commands(message)
        return

    # Build conversation history (last 10 messages)
    history = []
    async for msg in message.channel.history(limit=12, oldest_first=False):
        if msg.author.bot and msg.author != bot.user:
            continue
        role = "assistant" if msg.author == bot.user else "user"
        content = msg.content
        if content:
            history.append({"role": role, "content": f"{msg.author.display_name}: {content}"})

    history.reverse()  # Chronological order

    system_prompt = {
        "role": "system",
        "content": (
            "You are a helpful, friendly, and professional Discord support assistant powered by Grok. "
            "Answer questions clearly and concisely. Be polite and family-friendly. "
            "If the issue is complex or requires human action (bans, refunds, account changes, etc.), "
            "politely tell the user a staff member will assist them soon. "
            "Do not make up information. Keep responses under 1500 characters when possible."
        )
    }

    ai_messages = [system_prompt] + history[-10:]  # Last 10 turns

    async with message.channel.typing():
        reply = await get_ai_response(ai_messages)
        # Split long messages if needed
        if len(reply) > 1900:
            chunks = [reply[i:i+1900] for i in range(0, len(reply), 1900)]
            for chunk in chunks:
                await message.channel.send(chunk)
        else:
            await message.channel.send(reply)

    await bot.process_commands(message)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
