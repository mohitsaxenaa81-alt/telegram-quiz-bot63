# 🚀 Railway par Telegram Quiz Bot Deploy Karne Ka Complete Guide (Hinglish)

Aapka ye naya Telegram Quiz Bot Railway par bilkul free me 24/7 run hone ke liye ready hai. 

---

## 📋 Prerequisites (Requirements)
1. **GitHub Account** (https://github.com)
2. **Railway Account** (https://railway.app)
3. **MongoDB Atlas Account** (Free database - https://mongodb.com/cloud/atlas)
4. **Telegram Bot Token** (@BotFather se praapt karein)

---

## 🔹 STEP 1: MongoDB Atlas Free Database Setup
Railway par server restart hone par local SQLite database memory reset ho jaati hai, isliye persistent storage ke liye MongoDB Atlas Free Tier use karein.

1. **MongoDB Atlas** par login/register karein.
2. Naya **Free M0 Cluster** create karein.
3. Database Access me naya **User & Password** banayein.
4. Network Access me IP Address `0.0.0.0/0` (Allow Access from Anywhere) add karein.
5. **Connect** par click karke **Drivers (Node/Python)** choose karein aur Connection String copy karein:
   ```text
   mongodb+srv://<username>:<password>@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   ```
   *(Apna actual username aur password `<username>` aur `<password>` ki jagah replace karein)*

---

## 🔹 STEP 2: GitHub Repository Setup
1. GitHub par jaakar ek naya **Private / Public Repository** banayein (e.g., `telegram-quiz-bot-railway`).
2. Apne PC par terminal open karein aur is folder me jayein:
   ```bash
   cd C:\Users\HP\.gemini\antigravity\scratch\telegram_quiz_bot_railway
   ```
3. Git initialize karke GitHub par push karein:
   ```bash
   git init
   git add .
   git commit -m "Initial commit for Railway deployment"
   git branch -M main
   git remote add origin https://github.com/YOUR_GITHUB_USERNAME/telegram-quiz-bot-railway.git
   git push -u origin main
   ```

---

## 🔹 STEP 3: Railway par Project Deploy Karein
1. [Railway Dashboard](https://railway.app/dashboard) par jaakar **New Project** par click karein.
2. **Deploy from GitHub repo** select karein aur apni repo `telegram-quiz-bot-railway` choose karein.
3. Project deploy hone ka wait karein.
4. **Variables** tab par jaakar ye Environment Variables add karein:

| Variable Name | Value |
|---|---|
| `BOT_TOKEN` | Telegram Bot Token (`@BotFather` se milne wala token) |
| `MONGODB_URI` | MongoDB Atlas Connection String (Step 1 me copy ki gayi string) |

5. Variable add hote hi Railway automatic bot ko build karke restart karega.
6. **Deployments / Logs** tab par dekhein — jab `🚀 Public Telegram Quiz Bot starting...` likha aaye, aapka bot ready aur live ho chuka hai!

---

## 🧪 Local Machine Par Test Kaise Karein

Local PC par bina Railway ke run karne ke liye:
1. Terminal me folder me jayein:
   ```bash
   cd C:\Users\HP\.gemini\antigravity\scratch\telegram_quiz_bot_railway
   ```
2. Unit tests execute karein:
   ```bash
   python test_parser.py
   python test_db.py
   ```
3. Local test run:
   ```bash
   python quiz_bot.py
   ```
