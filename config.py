import os
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()

LONG_QUESTION_THRESHOLD = int(os.getenv("LONG_QUESTION_THRESHOLD", "200").strip())
