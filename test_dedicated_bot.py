import asyncio
import time
from telegram import Bot
from telegram.request import HTTPXRequest

token = '8968930189:AAEkphSCRH4Qah8jRLTqOD0jRrncevlRaoM'

async def main():
    # Long polling bot instance
    poll_bot = Bot(token, request=HTTPXRequest(connection_pool_size=10))
    # Dedicated quiz execution bot instance
    quiz_engine_bot = Bot(token, request=HTTPXRequest(connection_pool_size=20, connect_timeout=10.0, read_timeout=10.0))

    # Initialize both
    await poll_bot.initialize()
    await quiz_engine_bot.initialize()

    # Simulate getUpdates running on poll_bot in background
    async def run_poll_sim():
        try:
            # Simulate long-poll request hanging for 10 seconds
            print("[POLL BOT] Starting getUpdates long poll...")
            updates = await poll_bot.get_updates(timeout=10)
            print(f"[POLL BOT] getUpdates finished. Count: {len(updates)}")
        except Exception as e:
            print(f"[POLL BOT] getUpdates error: {e}")

    poll_task = asyncio.create_task(run_poll_sim())
    await asyncio.sleep(0.5)

    # Now test quiz_engine_bot sending API request concurrently while poll_bot is long-polling!
    print("[QUIZ ENGINE BOT] Sending get_me request concurrently...")
    t0 = time.monotonic()
    res = await quiz_engine_bot.get_me()
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    print(f"[QUIZ ENGINE BOT] get_me completed in {elapsed_ms:.2f}ms | Bot ID: {res.id}")

    await poll_task
    await poll_bot.shutdown()
    await quiz_engine_bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
