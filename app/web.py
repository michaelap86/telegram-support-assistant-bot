"""FastAPI app: Telegram webhook, health check and a small password-protected admin dashboard."""
import asyncio
import html
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .agent import Agent
from .bot import COMMANDS, build_dispatcher
from .config import Settings, get_settings
from .db import Db
from .kb import KnowledgeBase
from .llm import build_llm
from .seed import seed_demo

log = logging.getLogger("support-bot")
basic = HTTPBasic()


def build_state(settings: Settings, llm=None) -> dict:
    db = Db(settings.db_path)
    llm = llm or build_llm(settings)
    kb = KnowledgeBase(db, llm, settings.knowledge_dir)
    agent = Agent(settings, db, kb, llm)
    return {"settings": settings, "db": db, "kb": kb, "agent": agent, "dp": build_dispatcher(settings, db, agent)}


async def daily_reset(db: Db) -> None:
    """The public demo gets fresh orders and returns every night (UTC)."""
    while True:
        t = datetime.now(timezone.utc)
        nxt = (t + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((nxt - t).total_seconds())
        seed_demo(db)
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

    def admin_user(creds: HTTPBasicCredentials = Depends(basic)) -> str:
        ok = secrets.compare_digest(creds.username, settings.admin_user) and secrets.compare_digest(
            creds.password, settings.admin_password
        )
        if not ok:
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
        return creds.username

    @app.get("/admin", response_class=HTMLResponse)
    async def admin(request: Request, _: str = Depends(admin_user)):
        return HTMLResponse(render_admin(settings, request.app.state.s["db"]))

    return app


async def _feed(dp, bot, update) -> None:
    try:
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("update failed")


def render_admin(settings: Settings, db: Db) -> str:
    e = html.escape
    spend = db.spend_today()
    today = db.one("SELECT COALESCE(SUM(messages),0) m, COUNT(*) u, COALESCE(SUM(prompt_tokens+completion_tokens),0) t "
                   "FROM usage WHERE day=date('now')") or {"m": 0, "u": 0, "t": 0}
    fb = db.one("SELECT COALESCE(SUM(helpful),0) up, COUNT(*) n FROM feedback") or {"up": 0, "n": 0}
    handoffs = db.all("SELECT * FROM handoffs ORDER BY id DESC LIMIT 8")
    log_rows = db.all("SELECT chat_id, role, content, created_at FROM messages ORDER BY id DESC LIMIT 30")
    pct = min(100, round(spend / settings.global_daily_budget_usd * 100)) if settings.global_daily_budget_usd else 0
    satisfaction = f"{round(fb['up'] / fb['n'] * 100)}%" if fb["n"] else "no votes yet"

    handoff_html = "".join(
        f"<tr><td>#{h['id']}</td><td>{e(h['name'] or '')}</td><td>{e(h['summary'])}</td><td><span class='pill {h['status']}'>{h['status']}</span></td><td>{h['created_at']}</td></tr>"
        for h in handoffs
    ) or "<tr><td colspan=5 class=mute>No handoffs yet</td></tr>"
    log_html = "".join(
        f"<tr><td class=mute>{r['created_at'][11:16]}</td><td>user {str(r['chat_id'])[-4:]}</td>"
        f"<td><b>{'Customer' if r['role'] == 'user' else 'Bot'}</b></td><td>{e(r['content'][:220])}</td></tr>"
        for r in log_rows
    ) or "<tr><td colspan=4 class=mute>No conversations yet</td></tr>"

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{e(settings.shop_name)} support · admin</title>
<style>
:root{{--bg:#0f1420;--card:#171e2e;--line:#263049;--text:#e8ecf5;--mute:#8b96b0;--accent:#5b8cff;--ok:#3ecf8e;--warn:#f5a524}}
@media(prefers-color-scheme:light){{:root{{--bg:#f4f6fb;--card:#fff;--line:#e3e8f2;--text:#1a2133;--mute:#6b7690}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:28px 16px}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:15px;margin:28px 0 10px}}
.sub{{color:var(--mute);margin:0 0 22px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}}.k{{color:var(--mute);font-size:13px}}
.v{{font-size:26px;font-weight:650;margin-top:2px}}.bar{{height:8px;background:var(--line);border-radius:9px;margin-top:10px;overflow:hidden}}
.bar i{{display:block;height:100%;width:{pct}%;background:{'var(--warn)' if pct > 80 else 'var(--accent)'}}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}}
td{{padding:9px 12px;border-top:1px solid var(--line);vertical-align:top}}tr:first-child td{{border-top:0}}.mute{{color:var(--mute)}}
.pill{{padding:2px 9px;border-radius:99px;font-size:12px;background:var(--line)}}.pill.open{{background:var(--warn);color:#1a1200}}.pill.closed{{background:var(--ok);color:#04210f}}
.wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>{e(settings.shop_name)} support bot</h1><p class=sub>Live numbers for today (UTC). Refresh the page to update.</p>
<div class=grid>
<div class=card><div class=k>Messages today</div><div class=v>{today['m']}</div></div>
<div class=card><div class=k>People today</div><div class=v>{today['u']}</div></div>
<div class=card><div class=k>Tokens used today</div><div class=v>{today['t']:,}</div></div>
<div class=card><div class=k>Spend today</div><div class=v>${spend:.4f}</div><div class=bar><i></i></div><div class=k style="margin-top:6px">{pct}% of the ${settings.global_daily_budget_usd:.2f} daily budget</div></div>
<div class=card><div class=k>Helpful answers</div><div class=v>{satisfaction}</div><div class=k>{fb['n']} votes</div></div>
</div>
<h2>Human handoffs</h2><div class=wrap><table>{handoff_html}</table></div>
<h2>Latest messages</h2><div class=wrap><table>{log_html}</table></div>
</main></body></html>"""
