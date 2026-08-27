import os
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
try:
    OWNER_ID = int(os.getenv("OWNER_ID", "0").strip())
except ValueError:
    OWNER_ID = 0

try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0").strip())
except ValueError:
    GROUP_ID = 0

LONG_QUESTION_THRESHOLD = int(os.getenv("LONG_QUESTION_THRESHOLD", "200").strip())
