import os

# Secrets come from environment variables, with repository fallbacks so the
# bot boots without extra setup on Render.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8900663004:AAFmgdQeTop6ValsE6er-ZgxEu0n59_MNvU")
CHAT_ID = int(os.getenv("CHAT_ID", "-1003226050176"))

# Telegram channel/group button link shown under each forwarded OTP
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/bssyrxteamotp")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "OTP Group")

# Number-bot button shown under each forwarded OTP card
NUMBER_BOT_URL = os.getenv("NUMBER_BOT_URL", "https://t.me/bs_syrx69bot")
NUMBER_BOT_NAME = os.getenv("NUMBER_BOT_NAME", "BS SYRx NUMBER BOT")

# CR API (used only by the live forwarding loop, not the country sender).
API_URL = os.getenv("API_URL", "")
API_TOKEN = os.getenv("API_TOKEN", "")

# How many records to fetch from the API on each poll (max 200)
RECORDS = 100

# Poll interval in seconds
POLL_INTERVAL = 30

# File used to remember which OTPs were already forwarded (so we don't re-send on restart)
STATE_FILE = "state.json"

# Telegram user IDs allowed to control the bot (comma-separated). Empty = open.
OWNER_IDS = set(
    int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()
)
