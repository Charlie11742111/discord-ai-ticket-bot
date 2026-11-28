import os
import asyncio
import sqlite3
import random
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import re
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    AIOHTTP_AVAILABLE = False
    print("WARNING: aiohttp not installed. Nitrado/DayZ features disabled. Add aiohttp to requirements.txt and redeploy.")
from discord.ext import commands, tasks
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
XAI_API_KEY = os.getenv("XAI_API_KEY")

if not DISCORD_TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
    print("On Railway: go to the Variables tab and add DISCORD_TOKEN = your bot token.")
    print("Then redeploy.")
    raise SystemExit(1)

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
        await channel.send("AI is not configured. Ask an admin to set the XAI_API_KEY.")
        return
    try:
        context = ""
        try:
            context = await get_server_context(channel.guild)
        except:
            pass
        system = "You are a helpful support assistant in a Discord ticket. Be concise, friendly, and helpful."
        if context:
            system += f"\n\nServer information:\n{context}"
        response = await ai_client.chat.completions.create(
            model="grok-4",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message}
            ]
        )
        reply = response.choices[0].message.content.strip()
        if reply:
            await channel.send(reply)
        else:
            await channel.send("I couldn't generate a response. Please try again.")
    except Exception as e:
        print(f"AI error: {e}")
        await channel.send(f"AI error: {str(e)[:200]}")

@tasks.loop(minutes=5)
async def check_inactive_tickets():
    now = datetime.now(datetime.UTC)
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
    """CREATE TABLE IF NOT EXISTS economy_config (guild_id INTEGER PRIMARY KEY, work_min INTEGER DEFAULT 50, work_max INTEGER DEFAULT 150, work_cooldown INTEGER DEFAULT 3600, min_bet INTEGER DEFAULT 10, max_bet INTEGER DEFAULT 10000)""",
    """CREATE TABLE IF NOT EXISTS nitrado_config (guild_id INTEGER PRIMARY KEY, token TEXT, service_id TEXT, kill_channel INTEGER, hit_channel INTEGER, connect_channel INTEGER, online_channel INTEGER, alert_channel INTEGER, last_log_pos INTEGER DEFAULT 0, last_log_file TEXT)"""
]:
    cursor.execute(table)
db.commit()

# Safe migration for new alert_channel column
try:
    cursor.execute("ALTER TABLE nitrado_config ADD COLUMN alert_channel INTEGER")
    db.commit()
except sqlite3.OperationalError:
    pass  # Column already exists

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

honeypot_channels = set()
ai_context_channels = {}

async def get_server_context(guild):
    channel_id = ai_context_channels.get(guild.id)
    if not channel_id:
        return ""
    channel = guild.get_channel(channel_id)
    if not channel:
        return ""
    try:
        messages = [msg async for msg in channel.history(limit=20)]
        return "\n".join([m.content for m in messages if m.content])[:2000]
    except:
        return ""

@bot.event
async def on_guild_join(guild):
    overwrites_honey = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True)
    }
    honey = await guild.create_text_channel("general-chat", overwrites=overwrites_honey)
    honeypot_channels.add(honey.id)
    await honey.send("**Message in this channel and you will be banned.**")

    owner = guild.owner
    overwrites_ai = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        owner: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }
    ai_ch = await guild.create_text_channel("ai-server-info", overwrites=overwrites_ai)
    ai_context_channels[guild.id] = ai_ch.id
    await ai_ch.send("Only the server owner can see this channel. Write important info about the server here. The AI will use this when answering tickets.")

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
        ticket_last_activity[channel.id] = datetime.now(datetime.UTC)
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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    bot.add_view(TicketPanel())
    for guild in bot.guilds:
        await update_invite_cache(guild)
        try:
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to guild {guild.name} ({guild.id})")
        except Exception as e:
            print(f"Failed to sync to {guild.name}: {e}")
    try:
        await bot.tree.sync()
        print("Global command sync completed")
    except Exception as e:
        print(f"Global sync failed: {e}")
    check_inactive_tickets.start()
    if AIOHTTP_AVAILABLE:
        nitrado_log_poller.start()
    else:
        print("Nitrado poller not started (aiohttp missing)")

@bot.event
async def on_member_join(member):
    try:
        await member.edit(nick=member.name)
    except:
        pass

@bot.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.CategoryChannel):
        return
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel)):
        return
    if "◇《》" in channel.name:
        return
    try:
        await channel.edit(name=f"◇《》{channel.name}")
    except:
        pass

# In-memory tracking for suspicious activity (kill streaks)
recent_kills = {}  # guild_id -> {player_name: [timestamps]}

def clean_old_kills(guild_id: int, max_age_seconds: int = 180):
    if guild_id not in recent_kills:
        return
    now = datetime.now(datetime.UTC).timestamp()
    for player in list(recent_kills[guild_id].keys()):
        recent_kills[guild_id][player] = [t for t in recent_kills[guild_id][player] if now - t < max_age_seconds]
        if not recent_kills[guild_id][player]:
            del recent_kills[guild_id][player]

# Regex patterns for ADM log (based on official DayZ formats)
KILL_RE = re.compile(r'Player\s+"([^"]+)"\s*\(id=[^)]+\)\s+has been killed by\s+(?:player\s+)?"([^"]+)"', re.IGNORECASE)
HIT_RE = re.compile(r'Player\s+"([^"]+)"\s*\([^)]*\)\s*\[HP:\s*[\d.]+\]\s+hit by\s+"([^"]+)"\s+into\s+(\w+)\s+for\s+([\d.]+)\s+damage', re.IGNORECASE)
CONNECT_RE = re.compile(r'Player\s+"([^"]+)"\s*\(id=[^)]+\)\s+is\s+(connected|disconnected)', re.IGNORECASE)
PLAYERLIST_RE = re.compile(r'PlayerList log:\s*(\d+)\s+players', re.IGNORECASE)

async def download_log_content(token: str, service_id: str, file_path: str) -> str:
    data = await nitrado_request(token, "GET", f"/services/{service_id}/gameservers/file_server/download", params={"file": file_path})
    token_info = data.get("data", {}).get("token", {})
    url = token_info.get("url")
    if not url:
        raise Exception("No download URL returned")
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

@tasks.loop(minutes=2)
async def nitrado_log_poller():
    cursor.execute("SELECT guild_id, token, service_id FROM nitrado_config WHERE token IS NOT NULL AND service_id IS NOT NULL")
    for guild_id, token, service_id in cursor.fetchall():
        try:
            config = await get_nitrado_config(guild_id)
            if not config:
                continue
            gs_data = await nitrado_request(token, "GET", f"/services/{service_id}/gameservers")
            gs = gs_data.get("data", {}).get("gameserver", {})
            log_files = gs.get("game_specific", {}).get("log_files", [])
            if not log_files:
                continue
            adm_files = [f for f in log_files if isinstance(f, str) and f.lower().endswith(".adm") or (isinstance(f, dict) and str(f.get("path","")).lower().endswith(".adm"))]
            if not adm_files:
                continue
            latest = adm_files[-1]
            if isinstance(latest, dict):
                latest = latest.get("path") or latest.get("file") or ""
            if not latest:
                continue
            username = gs.get("username", "")
            candidates = [
                latest,
                f"/games/{username}/noftp/dayzps/config/{latest.split('/')[-1]}" if username else None,
                f"/games/{username}/noftp/dayzxb/config/{latest.split('/')[-1]}" if username else None,
            ]
            content = None
            used_path = None
            for path in candidates:
                if not path:
                    continue
                try:
                    content = await download_log_content(token, service_id, path)
                    used_path = path
                    break
                except:
                    continue
            if not content:
                continue
            lines = content.splitlines()
            last_pos = config["last_log_pos"]
            if config["last_log_file"] != used_path:
                last_pos = 0
            new_lines = lines[last_pos:]
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            kill_ch = guild.get_channel(config["kill_channel"]) if config["kill_channel"] else None
            hit_ch = guild.get_channel(config["hit_channel"]) if config["hit_channel"] else None
            connect_ch = guild.get_channel(config["connect_channel"]) if config["connect_channel"] else None
            online_ch = guild.get_channel(config["online_channel"]) if config["online_channel"] else None
            alert_ch = guild.get_channel(config["alert_channel"]) if config.get("alert_channel") else None
            for line in new_lines:
                if not line.strip():
                    continue
                m = KILL_RE.search(line)
                if m:
                    victim, killer = m.group(1), m.group(2)
                    if kill_ch:
                        embed = discord.Embed(title="💀 Kill Feed", description=f"**{killer}** killed **{victim}**", color=discord.Color.red())
                        embed.set_footer(text="DayZ via Nitrado")
                        await kill_ch.send(embed=embed)
                    if alert_ch and killer.lower() not in ("unknown", "zombie", "infected", "animal"):
                        now_ts = datetime.now(datetime.UTC).timestamp()
                        if guild_id not in recent_kills:
                            recent_kills[guild_id] = {}
                        if killer not in recent_kills[guild_id]:
                            recent_kills[guild_id][killer] = []
                        recent_kills[guild_id][killer].append(now_ts)
                        clean_old_kills(guild_id)
                        kills_in_window = len(recent_kills[guild_id].get(killer, []))
                        if kills_in_window >= 3:
                            alert_embed = discord.Embed(
                                title="⚠️ Suspicious Kill Streak",
                                description=f"**{killer}** has gotten **{kills_in_window}** kills in a short time.\nPossible lag switch or other unusual activity.",
                                color=discord.Color.gold()
                            )
                            alert_embed.set_footer(text="Console server alert — check logs and ban via Nitrado if needed")
                            try:
                                await alert_ch.send(embed=alert_embed)
                            except:
                                pass
                            recent_kills[guild_id][killer] = []
                    continue
                m = HIT_RE.search(line)
                if m and hit_ch:
                    victim, attacker, zone, dmg = m.group(1), m.group(2), m.group(3), m.group(4)
                    embed = discord.Embed(title="🩸 Hit Feed", description=f"**{attacker}** hit **{victim}** in the **{zone}** for **{dmg}** damage", color=discord.Color.orange())
                    await hit_ch.send(embed=embed)
                    continue
                m = CONNECT_RE.search(line)
                if m and connect_ch:
                    name, action = m.group(1), m.group(2)
                    emoji = "🟢" if action == "connected" else "🔴"
                    await connect_ch.send(f"{emoji} **{name}** {action}")
                    continue
                if "PlayerList log:" in line and online_ch:
                    players = re.findall(r'Player\s+"([^"]+)"', line)
                    if players:
                        embed = discord.Embed(title=f"👥 Online Players ({len(players)})", description="\n".join(f"• {p}" for p in players[:25]), color=discord.Color.green())
                        if len(players) > 25:
                            embed.description += f"\n... and {len(players)-25} more"
                        await online_ch.send(embed=embed)
            save_nitrado_config(guild_id, last_log_pos=len(lines), last_log_file=used_path)
        except Exception as e:
            print(f"Nitrado poller error for guild {guild_id}: {e}")

@nitrado_log_poller.before_loop
async def before_nitrado_poller():
    await bot.wait_until_ready()

@bot.tree.command(name="nitradosetup", description="Set up Nitrado DayZ connection (admin only)")
@commands.has_permissions(administrator=True)
async def nitradosetup(interaction: discord.Interaction, token: str, service_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await nitrado_request(token, "GET", f"/services/{service_id}/gameservers")
        name = data.get("data", {}).get("gameserver", {}).get("game_human", "Unknown")
        save_nitrado_config(interaction.guild.id, token=token, service_id=service_id)
        await interaction.followup.send(f"✅ Nitrado connected!\nServer: **{name}** (ID: `{service_id}`)\n\nNow set feed channels with `/setkillfeed`, `/sethitfeed`, etc.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed: {e}", ephemeral=True)

@bot.tree.command(name="setkillfeed", description="Set the kill feed channel")
@commands.has_permissions(administrator=True)
async def setkillfeed(interaction: discord.Interaction, channel: discord.TextChannel):
    save_nitrado_config(interaction.guild.id, kill_channel=channel.id)
    await interaction.response.send_message(f"Kill feed channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="sethitfeed", description="Set the hit feed channel")
@commands.has_permissions(administrator=True)
async def sethitfeed(interaction: discord.Interaction, channel: discord.TextChannel):
    save_nitrado_config(interaction.guild.id, hit_channel=channel.id)
    await interaction.response.send_message(f"Hit feed channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setconnectfeed", description="Set the connect/disconnect feed channel")
@commands.has_permissions(administrator=True)
async def setconnectfeed(interaction: discord.Interaction, channel: discord.TextChannel):
    save_nitrado_config(interaction.guild.id, connect_channel=channel.id)
    await interaction.response.send_message(f"Connect feed channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setonlinechannel", description="Set the online players channel")
@commands.has_permissions(administrator=True)
async def setonlinechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    save_nitrado_config(interaction.guild.id, online_channel=channel.id)
    await interaction.response.send_message(f"Online list channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setalertchannel", description="Set the channel for suspicious activity alerts (console lag switch etc.)")
@commands.has_permissions(administrator=True)
async def setalertchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    save_nitrado_config(interaction.guild.id, alert_channel=channel.id)
    await interaction.response.send_message(
        f"⚠️ Alert channel set to {channel.mention}\n\nThe bot will post here when it detects possible suspicious kill streaks from the DayZ logs.",
        ephemeral=True
    )

@bot.tree.command(name="online", description="Show currently online DayZ players")
async def online(interaction: discord.Interaction):
    config = await get_nitrado_config(interaction.guild.id)
    if not config or not config["token"]:
        return await interaction.response.send_message("Nitrado not set up.", ephemeral=True)
    await interaction.response.defer()
    try:
        data = await nitrado_request(config["token"], "GET", f"/services/{config['service_id']}/gameservers")
        gs = data.get("data", {}).get("gameserver", {})
        status = gs.get("status", "unknown")
        players = gs.get("query", {}).get("players", []) or []
        player_names = [p.get("name", "Unknown") for p in players] if isinstance(players, list) else []
        embed = discord.Embed(title="👥 DayZ Server Online", color=discord.Color.green())
        embed.add_field(name="Status", value=status.title(), inline=True)
        embed.add_field(name="Players", value=f"{len(player_names)} online", inline=True)
        if player_names:
            embed.description = "\n".join(f"• {n}" for n in player_names[:30])
        else:
            embed.description = "No detailed player list available (enable Player List Logging in Nitrado for better results)."
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}")

@bot.tree.command(name="serverstatus", description="Check DayZ server status")
async def serverstatus(interaction: discord.Interaction):
    config = await get_nitrado_config(interaction.guild.id)
    if not config or not config["token"]:
        return await interaction.response.send_message("Nitrado not set up.", ephemeral=True)
    try:
        data = await nitrado_request(config["token"], "GET", f"/services/{config['service_id']}/gameservers")
        gs = data.get("data", {}).get("gameserver", {})
        embed = discord.Embed(title="🖥️ DayZ Server Status", color=discord.Color.blue())
        embed.add_field(name="Name", value=gs.get("game_human", "Unknown"), inline=False)
        embed.add_field(name="Status", value=gs.get("status", "unknown").title(), inline=True)
        embed.add_field(name="IP", value=gs.get("ip", "N/A"), inline=True)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"Error: {e}", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    if message.channel.id in honeypot_channels:
        try:
            await message.author.ban(reason="Messaged in honeypot channel")
            await message.delete()
        except:
            pass
        return
    if "ticket-" in message.channel.name:
        ticket_last_activity[message.channel.id] = datetime.now(datetime.UTC)
        await ai_reply_in_ticket(message.channel, message.content)
    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
