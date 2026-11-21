import asyncio
import logging

import httpx

from .bot import Bot, Config
from .llm import OpenRouter
from .store import Store
from .telegram import Api


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs contain the bot token
    config = Config.from_env()
    async with httpx.AsyncClient() as client:
        bot = Bot(config, Api(config.telegram_token, client),
                  OpenRouter(config.openrouter_key, config.model, client, config.max_tokens), Store(config.database))
        try:
            await bot.run()
        finally:
            await bot.stop()


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
