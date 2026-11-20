import os
import asyncio
import sqlite3
import random
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
        await channel.send(response.choices[0].message.content)
    except Exception as e:
        print(f"AI error: {e}")

@tasks.loop(minutes=5)
async def check_inactive_tickets():
    now = datetime.utcnow()
    for channel_id, last_time in list(ticket_last_activity.items()):
        if (now - last_time).total_seconds() > 3600:
            channel = bot.get_channel(channel_id)
            if channel and "ticket-" in channel.name:
                try:
                    await channel.send("This ticket has been inactive for 1 hour and is now closing.")
                    await asyncio.sleep(3)
                    await channel.delete()
                except:
                    pass
            ticket_last_activity.pop(channel_id, None)

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

cursor.execute("""
CREATE TABLE IF NOT EXISTS economy (
    guild_id INTEGER,
    user_id INTEGER,
    balance INTEGER DEFAULT 0,
    bank INTEGER DEFAULT 0,
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

cursor.execute("""
CREATE TABLE IF NOT EXISTS economy_config (
    guild_id INTEGER PRIMARY KEY,
    work_min INTEGER DEFAULT 50,
    work_max INTEGER DEFAULT 150,
    work_cooldown INTEGER DEFAULT 3600,
    min_bet INTEGER DEFAULT 10,
    max_bet INTEGER DEFAULT 10000
)
""")

db.commit()

def get_config(guild_id):
    cursor.execute("SELECT work_min, work_max, work_cooldown, min_bet, max_bet FROM economy_config WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    if row:
        return {"work_min": row[0], "work_max": row[1], "work_cooldown": row[2], "min_bet": row[3], "max_bet": row[4]}
    return {"work_min": 50, "work_max": 150, "work_cooldown": 3600, "min_bet": 10, "max_bet": 10000}

def get_balances(guild_id, user_id):
    cursor.execute("SELECT balance, bank FROM economy WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    row = cursor.fetchone()
    return (row[0], row[1]) if row else (0, 0)

def get_balance(guild_id, user_id):
    return get_balances(guild_id, user_id)[0]

def get_bank(guild_id, user_id):
    return get_balances(guild_id, user_id)[1]

def set_balance(guild_id, user_id, amount):
    cursor.execute("""
        INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = ?
    """, (guild_id, user_id, amount, amount))
    db.commit()

def set_bank(guild_id, user_id, amount):
    cursor.execute("""
        INSERT INTO economy (guild_id, user_id, bank) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET bank = ?
    """, (guild_id, user_id, amount, amount))
    db.commit()

def add_balance(guild_id, user_id, amount):
    current = get_balance(guild_id, user_id)
    set_balance(guild_id, user_id, max(0, current + amount))

def add_bank(guild_id, user_id, amount):
    current = get_bank(guild_id, user_id)
    set_bank(guild_id, user_id, max(0, current + amount))

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
            return await interaction.followup.send("Ticket category not set. Use `/setup`.", ephemeral=True)
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
        try:
            await interaction.channel.delete()
        except:
            pass

class ApplicationSelect(discord.ui.Select):
    def __init__(self, apps: List[tuple]):
        options = [discord.SelectOption(label=app[2], value=str(app[0])) for app in apps[:15]]
        super().__init__(placeholder="Choose an application", options=options, custom_id="app_select")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Application started.", ephemeral=True)

class ApplicationView(discord.ui.View):
    def __init__(self, apps: List[tuple], style: str = "button"):
        super().__init__(timeout=None)
        if style == "dropdown":
            self.add_item(ApplicationSelect(apps))
        else:
            for app in apps[:15]:
                btn = discord.ui.Button(label=app[2], custom_id=f"app_{app[0]}", style=discord.ButtonStyle.primary)
                self.add_item(btn)

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
            if inv.uses > before and inv.inviter:
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

@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel)):
        return
    if channel.name.startswith("◇《》"):
        return
    try:
        await channel.edit(name=f"◇《》{channel.name}")
    except:
        pass

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
    embed = discord.Embed(title="Applications", description="Select an application below.", color=discord.Color.gold())
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
    try:
        await member.ban(reason=reason)
        await interaction.response.send_message(f"Banned {member.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="kick", description="Kick a member")
@commands.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await interaction.response.send_message(f"Kicked {member.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a member")
@commands.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        await interaction.response.send_message(f"Timed out {member.mention}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="invites", description="Check invite count")
async def invites(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    cursor.execute("SELECT invite_count FROM invites WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, user.id))
    row = cursor.fetchone()
    count = row[0] if row else 0
    await interaction.response.send_message(f"{user.mention} has invited **{count}** people.", ephemeral=True)

@bot.tree.command(name="inviteleaderboard", description="Show the top inviters")
async def inviteleaderboard(interaction: discord.Interaction, limit: int = 10):
    limit = max(1, min(25, limit))
    cursor.execute("""
        SELECT user_id, invite_count FROM invites
        WHERE guild_id = ? AND invite_count > 0
        ORDER BY invite_count DESC LIMIT ?
    """, (interaction.guild.id, limit))
    rows = cursor.fetchall()
    if not rows:
        return await interaction.response.send_message("No invite data yet.", ephemeral=True)
    embed = discord.Embed(title=f"Top {limit} Inviters", color=discord.Color.gold())
    desc = ""
    for i, (user_id, count) in enumerate(rows, 1):
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        desc += f"**{i}.** {name} — **{count}** invites\n"
    embed.description = desc
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="balance", description="Check balance")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    cash, bank = get_balances(interaction.guild.id, user.id)
    await interaction.response.send_message(f"{user.mention} has **{cash}** cash and **{bank}** in the bank.", ephemeral=True)

@bot.tree.command(name="work", description="Work to earn coins")
async def work(interaction: discord.Interaction):
    cfg = get_config(interaction.guild.id)
    cursor.execute("SELECT last_work FROM economy WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
    row = cursor.fetchone()
    now = datetime.utcnow()
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        if (now - last).total_seconds() < cfg["work_cooldown"]:
            remaining = int(cfg["work_cooldown"] - (now - last).total_seconds())
            return await interaction.response.send_message(f"You can work again in {remaining // 60} minutes.", ephemeral=True)
    earned = random.randint(cfg["work_min"], cfg["work_max"])
    add_balance(interaction.guild.id, interaction.user.id, earned)
    cursor.execute("""
        INSERT INTO economy (guild_id, user_id, last_work) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET last_work = ?
    """, (interaction.guild.id, interaction.user.id, now.isoformat(), now.isoformat()))
    db.commit()
    await interaction.response.send_message(f"You worked and earned **{earned}** coins!", ephemeral=True)

@bot.tree.command(name="deposit", description="Deposit coins into your bank")
async def deposit(interaction: discord.Interaction, amount: int):
    cash = get_balance(interaction.guild.id, interaction.user.id)
    if amount <= 0 or amount > cash:
        return await interaction.response.send_message("Invalid amount.", ephemeral=True)
    add_balance(interaction.guild.id, interaction.user.id, -amount)
    add_bank(interaction.guild.id, interaction.user.id, amount)
    await interaction.response.send_message(f"Deposited **{amount}** coins into your bank.", ephemeral=True)

@bot.tree.command(name="withdraw", description="Withdraw coins from your bank")
async def withdraw(interaction: discord.Interaction, amount: int):
    bank = get_bank(interaction.guild.id, interaction.user.id)
    if amount <= 0 or amount > bank:
        return await interaction.response.send_message("Invalid amount.", ephemeral=True)
    add_bank(interaction.guild.id, interaction.user.id, -amount)
    add_balance(interaction.guild.id, interaction.user.id, amount)
    await interaction.response.send_message(f"Withdrew **{amount}** coins from your bank.", ephemeral=True)

@bot.tree.command(name="setroleincome", description="Set passive income for a role")
@commands.has_permissions(administrator=True)
async def setroleincome(interaction: discord.Interaction, role: discord.Role, amount: int):
    cursor.execute("""
        INSERT INTO role_income (guild_id, role_id, amount) VALUES (?, ?, ?)
        ON CONFLICT(guild_id, role_id) DO UPDATE SET amount = ?
    """, (interaction.guild.id, role.id, amount, amount))
    db.commit()
    await interaction.response.send_message(f"{role.mention} now gives **{amount}** coins.", ephemeral=True)

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

@bot.tree.command(name="seteconomy", description="Configure economy settings")
@commands.has_permissions(administrator=True)
async def seteconomy(interaction: discord.Interaction, work_min: int = None, work_max: int = None, work_cooldown: int = None, min_bet: int = None, max_bet: int = None):
    cfg = get_config(interaction.guild.id)
    if work_min is not None: cfg["work_min"] = work_min
    if work_max is not None: cfg["work_max"] = work_max
    if work_cooldown is not None: cfg["work_cooldown"] = work_cooldown
    if min_bet is not None: cfg["min_bet"] = min_bet
    if max_bet is not None: cfg["max_bet"] = max_bet
    cursor.execute("""
        INSERT INTO economy_config (guild_id, work_min, work_max, work_cooldown, min_bet, max_bet)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            work_min = excluded.work_min,
            work_max = excluded.work_max,
            work_cooldown = excluded.work_cooldown,
            min_bet = excluded.min_bet,
            max_bet = excluded.max_bet
    """, (interaction.guild.id, cfg["work_min"], cfg["work_max"], cfg["work_cooldown"], cfg["min_bet"], cfg["max_bet"]))
    db.commit()
    await interaction.response.send_message(f"Economy updated: Work {cfg['work_min']}-{cfg['work_max']}, cooldown {cfg['work_cooldown']}s, bets {cfg['min_bet']}-{cfg['max_bet']}", ephemeral=True)

@bot.tree.command(name="coinflip", description="Gamble on coinflip")
async def coinflip(interaction: discord.Interaction, amount: int, side: str):
    cfg = get_config(interaction.guild.id)
    side = side.lower()
    if side not in ["heads", "tails"]:
        return await interaction.response.send_message("Choose heads or tails.", ephemeral=True)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"]:
        return await interaction.response.send_message(f"Bet must be between {cfg['min_bet']} and {cfg['max_bet']}.", ephemeral=True)
    bal = get_balance(interaction.guild.id, interaction.user.id)
    if amount > bal:
        return await interaction.response.send_message("Not enough cash.", ephemeral=True)
    result = random.choice(["heads", "tails"])
    if result == side:
        add_balance(interaction.guild.id, interaction.user.id, amount)
        await interaction.response.send_message(f"It was **{result}**! You won **{amount}** coins!", ephemeral=True)
    else:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        await interaction.response.send_message(f"It was **{result}**. You lost **{amount}** coins.", ephemeral=True)

@bot.tree.command(name="blackjack", description="Play blackjack")
async def blackjack(interaction: discord.Interaction, amount: int):
    cfg = get_config(interaction.guild.id)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"]:
        return await interaction.response.send_message(f"Bet must be between {cfg['min_bet']} and {cfg['max_bet']}.", ephemeral=True)
    bal = get_balance(interaction.guild.id, interaction.user.id)
    if amount > bal:
        return await interaction.response.send_message("Not enough cash.", ephemeral=True)
    def draw():
        return random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11])
    def total(cards):
        t = sum(cards)
        aces = cards.count(11)
        while t > 21 and aces:
            t -= 10
            aces -= 1
        return t
    player = [draw(), draw()]
    dealer = [draw(), draw()]
    p_total = total(player)
    d_total = total(dealer)
    if p_total == 21:
        win = int(amount * 1.5)
        add_balance(interaction.guild.id, interaction.user.id, win)
        return await interaction.response.send_message(f"Blackjack! You won **{win}** coins!", ephemeral=True)
    while d_total < 17:
        dealer.append(draw())
        d_total = total(dealer)
    if p_total > 21:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        await interaction.response.send_message(f"Bust! Lost **{amount}**.", ephemeral=True)
    elif d_total > 21 or p_total > d_total:
        add_balance(interaction.guild.id, interaction.user.id, amount)
        await interaction.response.send_message(f"You win! {p_total} vs {d_total}. Won **{amount}**.", ephemeral=True)
    elif p_total == d_total:
        await interaction.response.send_message(f"Push. Both {p_total}.", ephemeral=True)
    else:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        await interaction.response.send_message(f"Dealer wins. {p_total} vs {d_total}. Lost **{amount}**.", ephemeral=True)

@bot.tree.command(name="roulette", description="Play roulette")
async def roulette(interaction: discord.Interaction, amount: int, bet: str):
    cfg = get_config(interaction.guild.id)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"]:
        return await interaction.response.send_message(f"Bet must be between {cfg['min_bet']} and {cfg['max_bet']}.", ephemeral=True)
    bal = get_balance(interaction.guild.id, interaction.user.id)
    if amount > bal:
        return await interaction.response.send_message("Not enough cash.", ephemeral=True)
    number = random.randint(0, 36)
    color = "red" if number % 2 == 1 and number != 0 else ("black" if number != 0 else "green")
    bet = bet.lower()
    win = False
    payout = 0
    if bet.isdigit() and int(bet) == number:
        win = True
        payout = amount * 35
    elif bet in ["red", "black"] and bet == color:
        win = True
        payout = amount
    elif bet == "even" and number % 2 == 0 and number != 0:
        win = True
        payout = amount
    elif bet == "odd" and number % 2 == 1:
        win = True
        payout = amount
    if win:
        add_balance(interaction.guild.id, interaction.user.id, payout)
        await interaction.response.send_message(f"Ball landed on **{number} {color}**. You won **{payout}** coins!", ephemeral=True)
    else:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        await interaction.response.send_message(f"Ball landed on **{number} {color}**. You lost **{amount}** coins.", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if "ticket-" in message.channel.name and not message.author.bot:
        ticket_last_activity[message.channel.id] = datetime.utcnow()
        await ai_reply_in_ticket(message.channel, message.content)
    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
