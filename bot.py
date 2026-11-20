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

for table in [
    """CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER PRIMARY KEY, log_channel INTEGER, ticket_category INTEGER, automod_enabled INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, role_id INTEGER, questions TEXT, style TEXT DEFAULT 'button')""",
    """CREATE TABLE IF NOT EXISTS invites (guild_id INTEGER, user_id INTEGER, invite_count INTEGER DEFAULT 0, PRIMARY KEY (guild_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS economy (guild_id INTEGER, user_id INTEGER, balance INTEGER DEFAULT 0, bank INTEGER DEFAULT 0, last_work TEXT, PRIMARY KEY (guild_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS role_income (guild_id INTEGER, role_id INTEGER, amount INTEGER, PRIMARY KEY (guild_id, role_id))""",
    """CREATE TABLE IF NOT EXISTS economy_config (guild_id INTEGER PRIMARY KEY, work_min INTEGER DEFAULT 50, work_max INTEGER DEFAULT 150, work_cooldown INTEGER DEFAULT 3600, min_bet INTEGER DEFAULT 10, max_bet INTEGER DEFAULT 10000)"""
]:
    cursor.execute(table)
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
    cursor.execute("""INSERT INTO economy (guild_id, user_id, balance) VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = ?""", (guild_id, user_id, amount, amount))
    db.commit()

def set_bank(guild_id, user_id, amount):
    cursor.execute("""INSERT INTO economy (guild_id, user_id, bank) VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET bank = ?""", (guild_id, user_id, amount, amount))
    db.commit()

def add_balance(guild_id, user_id, amount):
    current = get_balance(guild_id, user_id)
    set_balance(guild_id, user_id, max(0, current + amount))

def add_bank(guild_id, user_id, amount):
    current = get_bank(guild_id, user_id)
    set_bank(guild_id, user_id, max(0, current + amount))

invite_cache = {}

async def update_invite_cache(guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
    except:
        invite_cache[guild.id] = {}

async def get_log_channel(guild):
    cursor.execute("SELECT log_channel FROM settings WHERE guild_id = ?", (guild.id,))
    row = cursor.fetchone()
    if row and row[0]:
        return guild.get_channel(row[0])
    return None

async def log_event(guild, embed):
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
            return await interaction.followup.send("Ticket category not set.", ephemeral=True)
        category = guild.get_channel(row[0])
        if not category:
            return await interaction.followup.send("Category not found.", ephemeral=True)
        name = f"◇《》ticket-{interaction.user.name.lower()}"
        if discord.utils.get(category.channels, name=name):
            return await interaction.followup.send("You already have a ticket.", ephemeral=True)
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

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing...")
        await asyncio.sleep(3)
        try:
            await interaction.channel.delete()
        except:
            pass

class BlackjackView(discord.ui.View):
    def __init__(self, player, amount, guild_id):
        super().__init__(timeout=60)
        self.player = player
        self.amount = amount
        self.guild_id = guild_id
        self.player_cards = [self.draw(), self.draw()]
        self.dealer_cards = [self.draw(), self.draw()]
        self.done = False

    def draw(self):
        return random.choice([2,3,4,5,6,7,8,9,10,10,10,10,11])

    def total(self, cards):
        t = sum(cards)
        aces = cards.count(11)
        while t > 21 and aces:
            t -= 10
            aces -= 1
        return t

    def card_str(self, cards, hide_second=False):
        symbols = {11: "A", 10: "10"}
        display = []
        for i, c in enumerate(cards):
            if hide_second and i == 1:
                display.append("🂠")
            else:
                display.append(symbols.get(c, str(c)))
        return " ".join(display)

    async def update_embed(self, interaction, ended=False):
        p_total = self.total(self.player_cards)
        d_total = self.total(self.dealer_cards)
        embed = discord.Embed(title="Blackjack", color=discord.Color.green() if not ended else discord.Color.gold())
        embed.add_field(name="Your Hand", value=f"{self.card_str(self.player_cards)} = **{p_total}**", inline=False)
        if ended:
            embed.add_field(name="Dealer Hand", value=f"{self.card_str(self.dealer_cards)} = **{d_total}**", inline=False)
        else:
            embed.add_field(name="Dealer Hand", value=f"{self.card_str(self.dealer_cards, hide_second=True)}", inline=False)
        embed.set_footer(text=f"Bet: {self.amount} coins")
        if ended:
            for item in self.children:
                item.disabled = True
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.player or self.done:
            return await interaction.response.defer()
        self.player_cards.append(self.draw())
        if self.total(self.player_cards) > 21:
            self.done = True
            add_balance(self.guild_id, self.player.id, -self.amount)
            await self.update_embed(interaction, ended=True)
            await interaction.followup.send("Bust! You lost.", ephemeral=True)
        else:
            await self.update_embed(interaction)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.player or self.done:
            return await interaction.response.defer()
        self.done = True
        while self.total(self.dealer_cards) < 17:
            self.dealer_cards.append(self.draw())
        p = self.total(self.player_cards)
        d = self.total(self.dealer_cards)
        if d > 21 or p > d:
            add_balance(self.guild_id, self.player.id, self.amount)
            result = f"You win! +{self.amount}"
        elif p == d:
            result = "Push!"
        else:
            add_balance(self.guild_id, self.player.id, -self.amount)
            result = f"Dealer wins. -{self.amount}"
        await self.update_embed(interaction, ended=True)
        await interaction.followup.send(result, ephemeral=True)

class RouletteView(discord.ui.View):
    def __init__(self, player, amount, guild_id):
        super().__init__(timeout=30)
        self.player = player
        self.amount = amount
        self.guild_id = guild_id

    async def spin(self, interaction, bet_type, bet_value=None):
        number = random.randint(0, 36)
        color = "red" if number % 2 == 1 and number != 0 else ("black" if number != 0 else "green")
        win = False
        payout = 0
        if bet_type == "number" and bet_value == number:
            win = True
            payout = self.amount * 35
        elif bet_type == "color" and bet_value == color:
            win = True
            payout = self.amount
        elif bet_type == "even" and number % 2 == 0 and number != 0:
            win = True
            payout = self.amount
        elif bet_type == "odd" and number % 2 == 1:
            win = True
            payout = self.amount
        embed = discord.Embed(title="Roulette", color=discord.Color.red() if color == "red" else discord.Color.dark_gray())
        embed.description = f"The ball landed on **{number} {color}**"
        if win:
            add_balance(self.guild_id, self.player.id, payout)
            embed.add_field(name="Result", value=f"You won **{payout}** coins!")
        else:
            add_balance(self.guild_id, self.player.id, -self.amount)
            embed.add_field(name="Result", value=f"You lost **{self.amount}** coins.")
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Red", style=discord.ButtonStyle.danger)
    async def red(self, interaction, button):
        if interaction.user != self.player:
            return await interaction.response.defer()
        await self.spin(interaction, "color", "red")

    @discord.ui.button(label="Black", style=discord.ButtonStyle.secondary)
    async def black(self, interaction, button):
        if interaction.user != self.player:
            return await interaction.response.defer()
        await self.spin(interaction, "color", "black")

    @discord.ui.button(label="Even", style=discord.ButtonStyle.primary)
    async def even(self, interaction, button):
        if interaction.user != self.player:
            return await interaction.response.defer()
        await self.spin(interaction, "even")

    @discord.ui.button(label="Odd", style=discord.ButtonStyle.primary)
    async def odd(self, interaction, button):
        if interaction.user != self.player:
            return await interaction.response.defer()
        await self.spin(interaction, "odd")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(TicketPanel())
    for guild in bot.guilds:
        await update_invite_cache(guild)
    await bot.tree.sync()
    check_inactive_tickets.start()

@bot.event
async def on_member_join(member):
    try:
        await member.edit(nick=member.name)
    except:
        pass

@bot.event
async def on_guild_channel_create(channel):
    if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.CategoryChannel)):
        if not channel.name.startswith("◇《》"):
            try:
                await channel.edit(name=f"◇《》{channel.name}")
            except:
                pass

@bot.tree.command(name="balance", description="Check balance")
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    cash, bank = get_balances(interaction.guild.id, user.id)
    await interaction.response.send_message(f"{user.mention} — Cash: **{cash}** | Bank: **{bank}**", ephemeral=True)

@bot.tree.command(name="work", description="Work to earn coins")
async def work(interaction: discord.Interaction):
    cfg = get_config(interaction.guild.id)
    cursor.execute("SELECT last_work FROM economy WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, interaction.user.id))
    row = cursor.fetchone()
    now = datetime.utcnow()
    if row and row[0]:
        last = datetime.fromisoformat(row[0])
        if (now - last).total_seconds() < cfg["work_cooldown"]:
            rem = int(cfg["work_cooldown"] - (now - last).total_seconds())
            return await interaction.response.send_message(f"Cooldown: {rem // 60}m left", ephemeral=True)
    earned = random.randint(cfg["work_min"], cfg["work_max"])
    add_balance(interaction.guild.id, interaction.user.id, earned)
    cursor.execute("""INSERT INTO economy (guild_id, user_id, last_work) VALUES (?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET last_work = ?""", (interaction.guild.id, interaction.user.id, now.isoformat(), now.isoformat()))
    db.commit()
    await interaction.response.send_message(f"You earned **{earned}** coins!", ephemeral=True)

@bot.tree.command(name="deposit", description="Deposit into bank")
async def deposit(interaction: discord.Interaction, amount: int):
    cash = get_balance(interaction.guild.id, interaction.user.id)
    if amount <= 0 or amount > cash:
        return await interaction.response.send_message("Invalid amount.", ephemeral=True)
    add_balance(interaction.guild.id, interaction.user.id, -amount)
    add_bank(interaction.guild.id, interaction.user.id, amount)
    await interaction.response.send_message(f"Deposited **{amount}**.", ephemeral=True)

@bot.tree.command(name="withdraw", description="Withdraw from bank")
async def withdraw(interaction: discord.Interaction, amount: int):
    bank = get_bank(interaction.guild.id, interaction.user.id)
    if amount <= 0 or amount > bank:
        return await interaction.response.send_message("Invalid amount.", ephemeral=True)
    add_bank(interaction.guild.id, interaction.user.id, -amount)
    add_balance(interaction.guild.id, interaction.user.id, amount)
    await interaction.response.send_message(f"Withdrew **{amount}**.", ephemeral=True)

@bot.tree.command(name="pay", description="Pay another member")
async def pay(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("Amount must be positive.", ephemeral=True)
    if member == interaction.user:
        return await interaction.response.send_message("You can't pay yourself.", ephemeral=True)
    cash = get_balance(interaction.guild.id, interaction.user.id)
    if amount > cash:
        return await interaction.response.send_message("Not enough cash.", ephemeral=True)
    add_balance(interaction.guild.id, interaction.user.id, -amount)
    add_balance(interaction.guild.id, member.id, amount)
    await interaction.response.send_message(f"You paid {member.mention} **{amount}** coins.", ephemeral=True)

@bot.tree.command(name="money", description="Admin: add or remove money")
@commands.has_permissions(administrator=True)
async def money(interaction: discord.Interaction, action: str, member: discord.Member, amount: int):
    action = action.lower()
    if action not in ["add", "remove"]:
        return await interaction.response.send_message("Use 'add' or 'remove'.", ephemeral=True)
    if action == "add":
        add_balance(interaction.guild.id, member.id, amount)
        await interaction.response.send_message(f"Added **{amount}** to {member.mention}.", ephemeral=True)
    else:
        add_balance(interaction.guild.id, member.id, -amount)
        await interaction.response.send_message(f"Removed **{amount}** from {member.mention}.", ephemeral=True)

@bot.tree.command(name="seteconomy", description="Configure economy")
@commands.has_permissions(administrator=True)
async def seteconomy(interaction: discord.Interaction, work_min: int = None, work_max: int = None, work_cooldown: int = None, min_bet: int = None, max_bet: int = None):
    cfg = get_config(interaction.guild.id)
    if work_min is not None: cfg["work_min"] = work_min
    if work_max is not None: cfg["work_max"] = work_max
    if work_cooldown is not None: cfg["work_cooldown"] = work_cooldown
    if min_bet is not None: cfg["min_bet"] = min_bet
    if max_bet is not None: cfg["max_bet"] = max_bet
    cursor.execute("""INSERT INTO economy_config (guild_id, work_min, work_max, work_cooldown, min_bet, max_bet) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET work_min=excluded.work_min, work_max=excluded.work_max, work_cooldown=excluded.work_cooldown, min_bet=excluded.min_bet, max_bet=excluded.max_bet""", (interaction.guild.id, cfg["work_min"], cfg["work_max"], cfg["work_cooldown"], cfg["min_bet"], cfg["max_bet"]))
    db.commit()
    await interaction.response.send_message("Economy settings updated.", ephemeral=True)

@bot.tree.command(name="coinflip", description="Coinflip with visual")
async def coinflip(interaction: discord.Interaction, amount: int, side: str):
    cfg = get_config(interaction.guild.id)
    side = side.lower()
    if side not in ["heads", "tails"]:
        return await interaction.response.send_message("Choose heads or tails.", ephemeral=True)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"] or amount > get_balance(interaction.guild.id, interaction.user.id):
        return await interaction.response.send_message("Invalid bet.", ephemeral=True)
    result = random.choice(["heads", "tails"])
    emoji = "🪙"
    embed = discord.Embed(title="Coinflip", description=f"You chose **{side}**\nResult: **{result}** {emoji}", color=discord.Color.gold())
    if result == side:
        add_balance(interaction.guild.id, interaction.user.id, amount)
        embed.add_field(name="Outcome", value=f"You won **{amount}**!")
    else:
        add_balance(interaction.guild.id, interaction.user.id, -amount)
        embed.add_field(name="Outcome", value=f"You lost **{amount}**.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="blackjack", description="Play visual blackjack")
async def blackjack(interaction: discord.Interaction, amount: int):
    cfg = get_config(interaction.guild.id)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"] or amount > get_balance(interaction.guild.id, interaction.user.id):
        return await interaction.response.send_message("Invalid bet.", ephemeral=True)
    view = BlackjackView(interaction.user, amount, interaction.guild.id)
    embed = discord.Embed(title="Blackjack", color=discord.Color.green())
    embed.add_field(name="Your Hand", value=view.card_str(view.player_cards) + f" = **{view.total(view.player_cards)}**", inline=False)
    embed.add_field(name="Dealer Hand", value=view.card_str(view.dealer_cards, hide_second=True), inline=False)
    embed.set_footer(text=f"Bet: {amount}")
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="roulette", description="Play visual roulette")
async def roulette(interaction: discord.Interaction, amount: int):
    cfg = get_config(interaction.guild.id)
    if amount < cfg["min_bet"] or amount > cfg["max_bet"] or amount > get_balance(interaction.guild.id, interaction.user.id):
        return await interaction.response.send_message("Invalid bet.", ephemeral=True)
    view = RouletteView(interaction.user, amount, interaction.guild.id)
    embed = discord.Embed(title="Roulette", description="Choose your bet!", color=discord.Color.blurple())
    embed.set_footer(text=f"Bet: {amount} coins")
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="setup", description="Setup settings")
@commands.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, log_channel: discord.TextChannel = None, ticket_category: discord.CategoryChannel = None):
    cursor.execute("""INSERT INTO settings (guild_id, log_channel, ticket_category) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET log_channel = COALESCE(?, log_channel), ticket_category = COALESCE(?, ticket_category)""", (interaction.guild.id, log_channel.id if log_channel else None, ticket_category.id if ticket_category else None, log_channel.id if log_channel else None, ticket_category.id if ticket_category else None))
    db.commit()
    await interaction.response.send_message("Updated.", ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Send ticket panel")
@commands.has_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    embed = discord.Embed(title="Need Help?", description="Click to open a ticket.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed, view=TicketPanel())

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    if "ticket-" in message.channel.name:
        ticket_last_activity[message.channel.id] = datetime.utcnow()
        await ai_reply_in_ticket(message.channel, message.content)
    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
