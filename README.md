# Telegram Quiz Bot (Railway Ready & MongoDB Atlas Support)

A features-rich, multi-user, public Telegram Quiz Bot designed for high performance and easy cloud hosting on Railway.

## ✨ Core Features
- **Public Usage**: Anyone can use `/start` to create quizzes or start quizzes in private/group chats.
- **Strict 4-Option Parser**: Supports Hindi + English bilingual text, `.txt` file uploads, and validates options and `✅` markers.
- **Multi-User Security**: Multi-user isolation using Telegram User ID. Users can only manage (`/myquizzes`, `/edit`, delete) their own quizzes.
- **Persistent Cloud DB**: Connects to MongoDB Atlas via `MONGODB_URI` for Railway, with seamless local SQLite fallback.
- **Zero-Drift Timer Engine**: High-accuracy Telegram QUIZ polls with automated leaderboard calculation.

---

## 🛠️ Quick Local Setup

1. **Navigate to the project folder:**
   ```bash
   cd C:\Users\HP\.gemini\antigravity\scratch\telegram_quiz_bot_railway
   ```

2. **Create and activate a Virtual Environment (Optional but recommended):**
   ```bash
   python -m venv venv
   # On Windows PowerShell:
   .\venv\Scripts\Activate.ps1
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment:**
   - Copy `.env.example` to `.env`
   - Fill in your `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).

5. **Run the Bot locally:**
   ```bash
   python quiz_bot.py
   ```

---

## ☁️ Deploying to Railway (Free Hosting)

Refer to [RAILWAY_GUIDE.md](file:///C:/Users/HP/.gemini/antigravity/scratch/telegram_quiz_bot_railway/RAILWAY_GUIDE.md) for detailed step-by-step instructions in Hinglish.
