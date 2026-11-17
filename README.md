# Discord AI Ticket Bot (Powered by Grok)

A simple Discord bot that lets users open private support tickets and automatically answers them using Grok AI from xAI.

## Features

- Button panel to create private tickets
- AI-powered replies in ticket channels using Grok
- Close ticket button
- Staff role support
- Persistent create button

## Setup

### 1. Create a Discord Application

1. Go to https://discord.com/developers/applications
2. Create a new application → Bot → Reset Token (copy it)
3. Enable **Message Content Intent** and **Server Members Intent** under Privileged Gateway Intents
4. Invite the bot with these permissions:
   - Manage Channels
   - Send Messages
   - Read Message History
   - Manage Messages
   - View Channels
   - Use Slash Commands

### 2. Get an xAI API Key

1. Go to https://console.x.ai
2. Create an API key
3. Copy it

### 3. Configure the Bot

1. Copy `.env.example` to `.env`
2. Fill in:
   - `DISCORD_TOKEN`
   - `XAI_API_KEY`
   - `TICKET_CATEGORY_ID` — Create a category called "Tickets" in your server, right-click → Copy ID (Developer Mode must be on)
   - `SUPPORT_ROLE_ID` (optional) — Role that can see tickets

### 4. Install & Run

```bash
python -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python bot.py
```

### 5. In Discord

1. Run `/setup` (as admin) in the channel where you want the ticket panel
2. Users click **Create Ticket**
3. The AI will automatically reply to messages in the ticket

## Notes

- The bot only replies in channels whose names start with `ticket-`
- Close buttons on existing tickets may need the bot to be online when created for full persistence after restarts (simple version)
- Change the model in `bot.py` if needed (`grok-4`, `grok-3`, etc.)
- Keep your `.env` file private!

Enjoy your AI-powered support bot!
