import asyncio
import time
import httpx
from telegram import Bot
from telegram.request import HTTPXRequest

token = '8968930189:AAEkphSCRH4Qah8jRLTqOD0jRrncevlRaoM'

async def main():
    # Force httpx to never keep idle connections alive (max_keepalive_connections=0)
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=30)
    req = HTTPXRequest(
        connection_pool_size=30,
        connect_timeout=5.0,
        read_timeout=10.0,
        httpx_kwargs={'limits': limits}
    )
    bot = Bot(token, request=req)
    await bot.initialize()

    # Call 1
    t0 = time.monotonic()
    me1 = await bot.get_me()
    print(f"First call elapsed: {((time.monotonic() - t0) * 1000.0):.2f} ms")

    # Idle for 12 seconds (simulating question timer)
    print("Simulating 12s question timer idle period...")
    await asyncio.sleep(12)

    # Call 2 (after idle period)
    t1 = time.monotonic()
    me2 = await bot.get_me()
    print(f"Call after 12s idle with max_keepalive_connections=0 elapsed: {((time.monotonic() - t1) * 1000.0):.2f} ms")

    await bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
