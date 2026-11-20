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

# Ticket activity tracking
ticket_last_activity = {}

async def ai_reply_in_ticket(channel, user_message):
    if not ai_client:
        return
    try:
        response = await ai_client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": "You are a helpful support assistant in a Discord ticket. Be concise and friendly."},
                {"role": "user", "content": user_message}
            ]
        )
        reply = response.choices[0].message.content
        await channel.send(reply)
    except Exception as e:
        print(f"AI error: {e}")

@tasks.loop(minutes=5)
async def check_inactive_tickets():
    now = datetime.utcnow()
    for channel_id, last_time in list(ticket_last_activity.items()):
        if (now - last_time).total_seconds() > 3600:  # 1 hour
            channel = bot.get_channel(channel_id)
            if channel and channel.name.startswith("◇《》ticket-"):
                try:
                    await channel.send("This ticket has been inactive for 1 hour and is now closing.")
                    await asyncio.sleep(3)
                    await channel.delete()
                except:
                    pass
            ticket_last_activity.pop(channel_id, None)

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

# Economy tables
cursor.execute("""
CREATE TABLE IF NOT EXISTS economy (
    guild_id INTEGER,
    user_id INTEGER,
    balance INTEGER DEFAULT 0,
    last_work TEXT,
    PRIMARY KEY (guild_id, user_id)
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS role_income (
    guild_id INTEGER,
    role_id INTEGER,
    amount INTEGER,
    PRIMARY KEY (guild_id, role_id)
)
""")
db.commit()

def get_balance(guild_id, user_id):
    cursor.execute("SELECT balance FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = cursor.fetchone()
    return row[0] if row else 0

def set_balance(guild_id, user_id, amount):
    cursor.execute("""
        INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = ?
    """, (guild_id, user_id, amount, amount))
    db.commit()

def add_balance(guild_id, user_id, amount):
    current = get_balance(guild_id, user_id)
    set_balance(guild_id, user_id, current + amount)

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

        name = f"◇《》ticket-{interaction.user.name.lower()}"
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
        ticket_last_activity[channel.id] = datetime.utcnow()
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
    check_inactive_tickets.start()

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

@bot.tree.command(name="balance", description="Check your or someone else's balance")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    bal = get_balance(interaction.guild.id, user.id)
    await interaction.response.send_message(f"{user.mention} has **{bal}** coins.", ephemeral=True)

@bot.tree.command(name="work", description="Work to earn coins (1 hour cooldown)")
async def work(interaction: discord.Interaction):
    cursor.execute("SELECT last_work FROM economy WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
    row = cursor.fetchone()
    now = datetime.utcnow()
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        if (now - last).total_seconds() < 3600:
            remaining = 3600 - int((now - last).total_seconds())
            return await interaction.response.send_message(f"You can work again in {remaining // 60} minutes.", ephemeral=True)
    earned = 50 + (interaction.user.id % 50)
    add_balance(interaction.guild.id, interaction.user.id, earned)
    cursor.execute("UPDATE economy SET last_work = ? WHERE guild_id = ? AND user_id = ?", (now.isoformat(), interaction.guild.id, interaction.user.id))
    db.commit()
    await interaction.response.send_message(f"You worked and earned **{earned}** coins!", ephemeral=True)

@bot.tree.command(name="setroleincome", description="Set passive income for a role")
@commands.has_permissions(administrator=True)
async def setroleincome(interaction: discord.Interaction, role: discord.Role, amount: int):
    cursor.execute("""
        INSERT INTO role_income (guild_id, role_id, amount) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, role_id) DO UPDATE SET amount = ?
    """, (interaction.guild.id, role.id, amount, amount))
    db.commit()
    await interaction.response.send_message(f"{role.mention} now gives **{amount}** coins per hour.", ephemeral=True)

@bot.tree.command(name="collect", description="Collect role income")
async def collect(interaction: discord.Interaction):
    total = 0
    for role in interaction.user.roles:
        cursor.execute("SELECT amount FROM role_income WHERE guild_id = ? AND role_id = ?", (interaction.guild.id, role.id))
        row = cursor.fetchone()
        if row:
            total += row[0]
    if total == 0:
        return await interaction.response.send_message("You have no role income to collect.", ephemeral=True)
    add_balance(interaction.guild.id, interaction.user.id, total)
    await interaction.response.send_message(f"Collected **{total}** coins from your roles!", ephemeral=True)

@bot.tree.command(name="coinflip", description="Gamble coins on a coinflip")
async def coinflip(interaction: discord.Interaction, amount: int, side: str):
    side = side.lower()
    if side not in ["heads", "tails"]:
        return await interaction.response.send_message("Choose heads or tails.", ephemeral=True)
    bal = get_balance(interaction.guild.id, interaction.user.id)
    if amount > bal or amount <= 0:
        return await interaction.response.send_message("Invalid amount.", ephemeral=True)
    import random
    result = random.choice(["heads", "tails"])
    if result == side:
        add_balance(interaction.guild.id, interaction.user.id, amount)
        await interaction.response.send_message(f"It was **{result}**! You won **{amount}** coins!", ephemeral=True)
    else:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        await interaction.response.send_message(f"It was **{result}**. You lost **{amount}** coins.", ephemeral=True)

@bot.tree.command(name="clonecategory", description="Clone a category and its channels (with roles/permissions) to another server")
@commands.has_permissions(administrator=True)
async def clonecategory(interaction: discord.Interaction, source_category: discord.CategoryChannel, target_guild_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        target_guild = bot.get_guild(int(target_guild_id))
        if not target_guild:
            return await interaction.followup.send("Bot is not in the target server or invalid guild ID.", ephemeral=True)

        role_map = {}
        for role in source_category.guild.roles:
            if role.is_default():
                role_map[role.id] = target_guild.default_role
                continue
            existing = discord.utils.get(target_guild.roles, name=role.name)
            if existing:
                role_map[role.id] = existing
            else:
                new_role = await target_guild.create_role(
                    name=role.name,
                    permissions=role.permissions,
                    colour=role.colour,
                    hoist=role.hoist,
                    mentionable=role.mentionable
                )
                role_map[role.id] = new_role

        cat_overwrites = {}
        for target, overwrite in source_category.overwrites.items():
            if isinstance(target, discord.Role) and target.id in role_map:
                cat_overwrites[role_map[target.id]] = overwrite

        new_category = await target_guild.create_category(name=source_category.name, overwrites=cat_overwrites)

        for channel in source_category.channels:
            overwrites = {}
            for target, overwrite in channel.overwrites.items():
                if isinstance(target, discord.Role) and target.id in role_map:
                    overwrites[role_map[target.id]] = overwrite

            if isinstance(channel, discord.TextChannel):
                await new_category.create_text_channel(
                    name=f"◇《》{channel.name}",
                    topic=channel.topic,
                    overwrites=overwrites,
                    nsfw=channel.nsfw,
                    slowmode_delay=channel.slowmode_delay
                )
            elif isinstance(channel, discord.VoiceChannel):
                await new_category.create_voice_channel(
                    name=f"◇《》{channel.name}",
                    overwrites=overwrites,
                    bitrate=min(channel.bitrate, 96000),
                    user_limit=channel.user_limit
                )

        await interaction.followup.send(f"Successfully cloned category **{source_category.name}** to {target_guild.name} with permissions!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error while cloning: {e}", ephemeral=True)

@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel, discord.StageChannel, discord.ForumChannel)):
        return
    if channel.name.startswith("◇《》"):
        return
    try:
        await channel.edit(name=f"◇《》{channel.name}")
    except:
        pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    # Auto-reply in tickets
    if message.channel.name.startswith("◇《》ticket-") and not message.author.bot:
        ticket_last_activity[message.channel.id] = datetime.utcnow()
        await ai_reply_in_ticket(message.channel, message.content)
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
