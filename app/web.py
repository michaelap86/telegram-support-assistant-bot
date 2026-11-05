"""FastAPI app: Telegram webhook, health check and the admin dashboard."""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from .admin import build_router
from .agent import Agent
from .bot import COMMANDS, build_dispatcher
from .config import Settings, get_settings
from .db import Db
from .kb import KnowledgeBase
from .llm import build_llm
from .samples import seed_samples
from .seed import seed_demo

log = logging.getLogger("support-bot")


def build_state(settings: Settings, llm=None) -> dict:
    db = Db(settings.db_path)
    llm = llm or build_llm(settings)
    kb = KnowledgeBase(db, llm, settings.knowledge_dir)
    agent = Agent(settings, db, kb, llm)
    return {"settings": settings, "db": db, "kb": kb, "agent": agent, "dp": build_dispatcher(settings, db, agent)}


async def daily_reset(db: Db) -> None:
    """The public demo gets fresh orders, returns and sample history every night (UTC)."""
    while True:
        t = datetime.now(timezone.utc)
        nxt = (t + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((nxt - t).total_seconds())
        seed_demo(db)
        seed_samples(db)
        log.info("demo data reset")


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = build_state(settings, llm)
        app.state.s = state
        db: Db = state["db"]
        if not db.one("SELECT 1 AS x FROM products LIMIT 1"):
            seed_demo(db)
        seed_samples(db)
        # Sections are stored at once; embeddings are computed in the background so a slow or failing
        # model API can never keep the app from starting (search uses keywords until they are ready).
        tasks = [asyncio.create_task(state["kb"].sync()), asyncio.create_task(daily_reset(db))]
        bot = None
        if settings.telegram_token:
            bot = Bot(settings.telegram_token, default=DefaultBotProperties())
            app.state.bot = bot
            await bot.set_my_commands(COMMANDS)
            if settings.public_url:
                await bot.set_webhook(
                    f"{settings.public_url.rstrip('/')}/telegram/webhook",
                    secret_token=settings.webhook_secret,
                    allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True,
                )
            else:  # local development: no public address, so poll Telegram instead
                await bot.delete_webhook()
                tasks.append(asyncio.create_task(state["dp"].start_polling(bot, handle_signals=False)))
        yield
        for t in tasks:
            t.cancel()
        if bot:
            await bot.session.close()

    app = FastAPI(title="Support bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_router(settings))

    @app.get("/")
    async def home():
        return RedirectResponse("/admin")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/telegram/webhook")
    async def webhook(request: Request):
        if not secrets.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), settings.webhook_secret):
            raise HTTPException(403)
        bot = getattr(request.app.state, "bot", None)
        if not bot:
            raise HTTPException(503)
        update = Update.model_validate(await request.json(), context={"bot": bot})
        # Answer Telegram at once and work in the background, so slow replies never trigger retries
        asyncio.create_task(_feed(request.app.state.s["dp"], bot, update))
        return {"ok": True}

    return app


async def _feed(dp, bot, update) -> None:
    try:
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("update failed")
