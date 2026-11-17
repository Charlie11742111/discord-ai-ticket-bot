# Discord AI Ticket + Applications + AutoMod Bot

A feature-rich Discord bot with:
- Ticket system with button panel
- Application system (up to 15) with buttons or dropdowns and role rewards
- AutoMod commands (ban, kick, timeout)
- Logging (joins, leaves, message deletes, automod actions)
- Invite tracking
- Custom embed creator
- Nickname lock on join (sets nickname to username)

## Setup

1. Create a Discord bot at https://discord.com/developers/applications
   - Enable **Message Content**, **Server Members**, and **Presence** intents
2. Get an optional xAI API key from https://console.x.ai (for AI ticket replies)
3. Copy `.env.example` to `.env` and fill in your token
4. Install:
   ```bash
   pip install -r requirements.txt
   ```
5. Run: `python bot.py`

## Commands (slash)

- `/setup` — Set log channel and ticket category
- `/ticketpanel` — Send ticket button panel
- `/createapplication` — Create an application (name, role, optional questions separated by `||`)
- `/applicationpanel` — Send applications panel
- `/embed` — Create a custom embed
- `/ban` `/kick` `/timeout` — Moderation
- `/invites` — Check invite count

## Notes

- Invite tracking is approximate (based on invite use increases).
- Application questions are stored but the full multi-step form can be expanded.
- Nickname locking works best if nickname changes are disabled in server settings.
- Database is stored in `bot.db` (created automatically).

Keep your `.env` private!
