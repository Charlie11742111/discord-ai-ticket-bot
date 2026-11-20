import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict

import discord
from discord.ext import commands, tasks
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")

if not DISCORD_TOKEN:
    raise ValueError("Missing DISCORD_TOKEN in .env")

# Optional AI (can run without it)
ai_client = None
if XAI_API_KEY:
    ai_client = AsyncOpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.invites = True
intents.moderation = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Database
db = sqlite3.connect("bot.db", check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY,
    log_channel INTEGER,
    ticket_category INTEGER,
    automod_enabled INTEGER DEFAULT 1
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    name TEXT,
    role_id INTEGER,
    questions TEXT,
    style TEXT DEFAULT 'button'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS invites (
    guild_id INTEGER,
    user_id INTEGER,
    invite_count INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
)
""")

db.commit()

# Invite cache
invite_cache: Dict[int, Dict[str, int]] = {}

async def update_invite_cache(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
    except:
        invite_cache[guild.id] = {}

async def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    cursor.execute("SELECT log_channel FROM settings WHERE guild_id = ?", (guild.id,))
    row = cursor.fetchone()
    if row and row[0]:
        return guild.get_channel(row[0])
    return None

async def log_event(guild: discord.Guild, embed: discord.Embed):
    channel = await get_log_channel(guild)
    if channel:
        try:
            await channel.send(embed=embed)
        except:
            pass

class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green, custom_id="open_ticket", emoji="🎟")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not guild:
            return

        cursor.execute("SELECT ticket_category FROM settings WHERE guild_id = ?", (guild.id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return await interaction.followup.send("Ticket category not set. Ask an admin to use `/setup tickets`.", ephemeral=True)

        category = guild.get_channel(row[0])
        if not category:
            return await interaction.followup.send("Ticket category not found.", ephemeral=True)

        name = f"ticket-{interaction.user.name.lower()}"
        if discord.utils.get(category.channels, name=name):
            return await interaction.followup.send("You already have an open ticket.", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        channel = await category.create_text_channel(name=name, overwrites=overwrites)
        embed = discord.Embed(title="Support Ticket", description=f"Hello {interaction.user.mention}, describe your issue.", color=discord.Color.green())
        await channel.send(embed=embed, view=TicketClose(channel.id))
        await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)

class TicketClose(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

class ApplicationSelect(discord.ui.Select):
    def __init__(self, apps: List[tuple]):
        options = [discord.SelectOption(label=app[2], value=str(app[0])) for app in apps[:15]]
        super().__init__(placeholder="Choose an application", options=options, custom_id="app_select")

    async def callback(self, interaction: discord.Interaction):
        app_id = int(self.values[0])
        cursor.execute("SELECT questions, role_id FROM applications WHERE id = ?", (app_id,))
        row = cursor.fetchone()
        if not row:
            return await interaction.response.send_message("Application not found.", ephemeral=True)

        questions = row[0].split("||") if row[0] else []
        await interaction.response.send_message(f"Starting application. Answer the questions in this channel or DM.", ephemeral=True)
        await interaction.followup.send("Questions will be asked one by one. (Full multi-step system can be expanded.)", ephemeral=True)

class ApplicationView(discord.ui.View):
    def __init__(self, apps: List[tuple], style: str = "button"):
        super().__init__(timeout=None)
        if style == "dropdown":
            self.add_item(ApplicationSelect(apps))
        else:
            for app in apps[:15]:
                btn = discord.ui.Button(label=app[2], custom_id=f"app_{app[0]}", style=discord.ButtonStyle.primary)
                btn.callback = self.make_callback(app[0])
                self.add_item(btn)

    def make_callback(self, app_id):
        async def callback(interaction: discord.Interaction):
            cursor.execute("SELECT questions, role_id, name FROM applications WHERE id = ?", (app_id,))
            row = cursor.fetchone()
            if not row:
                return await interaction.response.send_message("Application not found.", ephemeral=True)
            questions = row[0].split("||") if row[0] else []
            if not questions:
                role = interaction.guild.get_role(row[1])
                if role:
                    await interaction.user.add_roles(role)
                    await interaction.response.send_message(f"You received the {role.name} role!", ephemeral=True)
                else:
                    await interaction.response.send_message("Role not found.", ephemeral=True)
            else:
                await interaction.response.send_message(f"Application **{row[2]}** has questions. A full form system would ask them here.", ephemeral=True)
        return callback

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(TicketPanel())
    for guild in bot.guilds:
        await update_invite_cache(guild)
    await bot.tree.sync()
    print("Synced commands")

@bot.event
async def on_member_join(member: discord.Member):
    try:
        await member.edit(nick=member.name)
    except:
        pass

    try:
        invites_before = invite_cache.get(member.guild.id, {})
        invites_after = await member.guild.invites()
        for inv in invites_after:
            before = invites_before.get(inv.code, 0)
            if inv.uses > before:
                cursor.execute("""
                    INSERT INTO invites (guild_id, user_id, invite_count) VALUES (?, ?, 1)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET invite_count = invite_count + 1
                """, (member.guild.id, inv.inviter.id))
                db.commit()
                embed = discord.Embed(title="Member Joined", description=f"{member.mention} joined via invite from {inv.inviter.mention}", color=discord.Color.green())
                await log_event(member.guild, embed)
                break
        await update_invite_cache(member.guild)
    except:
        embed = discord.Embed(title="Member Joined", description=f"{member.mention} joined", color=discord.Color.green())
        await log_event(member.guild, embed)

@bot.event
async def on_member_remove(member: discord.Member):
    embed = discord.Embed(title="Member Left", description=f"{member.mention} left the server", color=discord.Color.red())
    await log_event(member.guild, embed)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    embed = discord.Embed(title="Message Deleted", description=f"**Author:** {message.author.mention}\n**Content:** {message.content[:1000] or '(embed/attachment)'}", color=discord.Color.orange())
    await log_event(message.guild, embed)

@bot.tree.command(name="setup", description="Setup bot settings")
@commands.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, log_channel: discord.TextChannel = None, ticket_category: discord.CategoryChannel = None):
    cursor.execute("""
        INSERT INTO settings (guild_id, log_channel, ticket_category) VALUES (?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET log_channel = COALESCE(?, log_channel), ticket_category = COALESCE(?, ticket_category)
    """, (interaction.guild.id, log_channel.id if log_channel else None, ticket_category.id if ticket_category else None,
          log_channel.id if log_channel else None, ticket_category.id if ticket_category else None))
    db.commit()
    await interaction.response.send_message("Settings updated!", ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Send the ticket panel")
@commands.has_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    embed = discord.Embed(title="Need Help?", description="Click the button below to open a private ticket.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=TicketPanel())

@bot.tree.command(name="createapplication", description="Create an application (max 15)")
@commands.has_permissions(administrator=True)
async def createapplication(interaction: discord.Interaction, name: str, role: discord.Role, questions: str = "", style: str = "button"):
    cursor.execute("SELECT COUNT(*) FROM applications WHERE guild_id = ?", (interaction.guild.id,))
    if cursor.fetchone()[0] >= 15:
        return await interaction.response.send_message("Maximum 15 applications reached.", ephemeral=True)
    cursor.execute("INSERT INTO applications (guild_id, name, role_id, questions, style) VALUES (?, ?, ?, ?, ?)",
                   (interaction.guild.id, name, role.id, questions, style))
    db.commit()
    await interaction.response.send_message(f"Application **{name}** created!", ephemeral=True)

@bot.tree.command(name="applicationpanel", description="Send the applications panel")
@commands.has_permissions(administrator=True)
async def applicationpanel(interaction: discord.Interaction):
    cursor.execute("SELECT id, guild_id, name, role_id, questions, style FROM applications WHERE guild_id = ?", (interaction.guild.id,))
    apps = cursor.fetchall()
    if not apps:
        return await interaction.response.send_message("No applications created yet.", ephemeral=True)
    style = apps[0][5] if apps else "button"
    embed = discord.Embed(title="Applications", description="Select an application below to apply for a role.", color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, view=ApplicationView(apps, style))

@bot.tree.command(name="embed", description="Create a custom embed")
@commands.has_permissions(administrator=True)
async def embed_cmd(interaction: discord.Interaction, title: str, description: str, color: str = "blurple"):
    colors = {"blurple": discord.Color.blurple(), "green": discord.Color.green(), "red": discord.Color.red(), "gold": discord.Color.gold()}
    embed = discord.Embed(title=title, description=description, color=colors.get(color.lower(), discord.Color.blurple()))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ban", description="Ban a member")
@commands.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    embed = discord.Embed(title="Member Banned", description=f"{member.mention} was banned by {interaction.user.mention}\n**Reason:** {reason}", color=discord.Color.red())
    await log_event(interaction.guild, embed)
    await interaction.response.send_message(f"Banned {member.mention}", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a member")
@commands.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    embed = discord.Embed(title="Member Kicked", description=f"{member.mention} was kicked by {interaction.user.mention}\n**Reason:** {reason}", color=discord.Color.orange())
    await log_event(interaction.guild, embed)
    await interaction.response.send_message(f"Kicked {member.mention}", ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a member")
@commands.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    await member.timeout(timedelta(minutes=minutes), reason=reason)
    embed = discord.Embed(title="Member Timed Out", description=f"{member.mention} was timed out for {minutes} minutes by {interaction.user.mention}", color=discord.Color.yellow())
    await log_event(interaction.guild, embed)
    await interaction.response.send_message(f"Timed out {member.mention}", ephemeral=True)

@bot.tree.command(name="invites", description="Check invite count")
async def invites(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    cursor.execute("SELECT invite_count FROM invites WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, user.id))
    row = cursor.fetchone()
    count = row[0] if row else 0
    await interaction.response.send_message(f"{user.mention} has invited **{count}** people.", ephemeral=True)

@bot.tree.command(name="inviteleaderboard", description="Show the top inviters in the server")
async def inviteleaderboard(interaction: discord.Interaction, limit: int = 10):
    if limit > 25:
        limit = 25
    if limit < 1:
        limit = 10
    cursor.execute("""
        SELECT user_id, invite_count FROM invites
        WHERE guild_id = ? AND invite_count > 0
        ORDER BY invite_count DESC
        LIMIT ?
    """, (interaction.guild.id, limit))
    rows = cursor.fetchall()
    if not rows:
        return await interaction.response.send_message("No invite data yet.", ephemeral=True)
    embed = discord.Embed(title=f"Top {limit} Inviters", color=discord.Color.gold())
    description = ""
    for i, (user_id, count) in enumerate(rows, 1):
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        description += f"**{i}.** {name} — **{count}** invites\n"
    embed.description = description
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clonecategory", description="Clone a category and its channels (with roles/permissions) to another server")
@commands.has_permissions(administrator=True)
async def clonecategory(interaction: discord.Interaction, source_category: discord.CategoryChannel, target_guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        target_guild = bot.get_guild(int(target_guild_id))
        if not target_guild:
            return await interaction.followup.send("Bot is not in the target server or invalid guild ID.", ephemeral=True)

        # Create the category in the target server
        new_category = await target_guild.create_category(name=source_category.name)

        role_map = {}  # Map old role IDs to new roles

        # Clone channels under the category
        for channel in source_category.channels:
            # Build overwrites, creating roles if needed
            overwrites = {}
            for target, overwrite in channel.overwrites.items():
                if isinstance(target, discord.Role):
                    # Find or create matching role in target guild
                    if target.id not in role_map:
                        existing = discord.utils.get(target_guild.roles, name=target.name)
                        if existing:
                            role_map[target.id] = existing
                        else:
                            # Create the role
                            new_role = await target_guild.create_role(
                                name=target.name,
                                permissions=target.permissions,
                                colour=target.colour,
                                hoist=target.hoist,
                                mentionable=target.mentionable
                            )
                            role_map[target.id] = new_role
                    overwrites[role_map[target.id]] = overwrite
                elif isinstance(target, discord.Member):
                    # Skip member-specific overwrites for simplicity
                    continue
                else:
                    overwrites[target] = overwrite

            if isinstance(channel, discord.TextChannel):
                await new_category.create_text_channel(
                    name=channel.name,
                    topic=channel.topic,
                    overwrites=overwrites,
                    nsfw=channel.nsfw,
                    slowmode_delay=channel.slowmode_delay
                )
            elif isinstance(channel, discord.VoiceChannel):
                await new_category.create_voice_channel(
                    name=channel.name,
                    overwrites=overwrites,
                    bitrate=channel.bitrate,
                    user_limit=channel.user_limit
                )

        await interaction.followup.send(f"Successfully cloned category **{source_category.name}** to {target_guild.name}!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error while cloning: {e}", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if any(word in message.content.lower() for word in ["badword1", "badword2"]):
        try:
            await message.delete()
            embed = discord.Embed(title="Automod Action", description=f"Deleted message from {message.author.mention} for rule break", color=discord.Color.red())
            await log_event(message.guild, embed)
        except:
            pass
    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
