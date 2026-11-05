"""Telegram side: commands, buttons, human handoff and the admin relay."""
import asyncio
import time
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .agent import Agent, Answer
from .config import Settings
from .db import Db, now

HANDOFF_TTL = "-2 hours"  # a handoff nobody answers is released after this long

WELCOME = (
    "Hi {name}! I'm the support assistant of {shop}.\n\n"
    "I can track an order, start a return, check prices and stock, and answer questions about "
    "shipping, warranty and payments. Just write to me like you would to a person.\n\n"
    "This is a demo shop with made-up data. Try order NG-10001 with alex@example.com."
)

HELP = (
    "Ask me anything about the shop. Some ideas:\n"
    "- Where is my order NG-10001? (email: alex@example.com)\n"
    "- Can I return my charger? (order NG-10003, email: jo@example.com)\n"
    "- Do you have earbuds under 70 euro?\n"
    "- How long is the warranty?\n\n"
    "/human - talk to a person\n"
    "/reset - forget our conversation\n"
    "/help - this message"
)

QUICK = {
    "order": "Where is my order?",
    "return": "I want to return something",
    "ship": "How much is shipping and how long does it take?",
    "warranty": "How long is the warranty?",
}


def quick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Track my order", callback_data="q:order"),
             InlineKeyboardButton(text="↩️ Start a return", callback_data="q:return")],
            [InlineKeyboardButton(text="🚚 Shipping", callback_data="q:ship"),
             InlineKeyboardButton(text="🛡 Warranty", callback_data="q:warranty")],
            [InlineKeyboardButton(text="🙋 Talk to a human", callback_data="q:human")],
        ]
    )


def feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👍", callback_data="fb:1"), InlineKeyboardButton(text="👎", callback_data="fb:0")]]
    )


class Throttle:
    """At most `n` messages per `seconds` from one person."""

    def __init__(self, n: int = 6, seconds: int = 12):
        self.n, self.seconds, self.seen = n, seconds, defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        q, t = self.seen[user_id], time.monotonic()
        while q and t - q[0] > self.seconds:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(t)
        return True


def build_dispatcher(settings: Settings, db: Db, agent: Agent) -> Dispatcher:
    router = Router()
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
    throttle = Throttle()

    def is_admin(chat_id: int) -> bool:
        return bool(settings.admin_chat_id) and chat_id == settings.admin_chat_id

    def open_handoff(chat_id: int) -> dict | None:
        db.run("UPDATE handoffs SET status='expired' WHERE status='open' AND created_at < datetime('now', ?)", (HANDOFF_TTL,))
        return db.one("SELECT * FROM handoffs WHERE chat_id=? AND status='open'", (chat_id,))

    async def to_admin(bot: Bot, handoff_id: int, text: str) -> None:
        sent = await bot.send_message(settings.admin_chat_id, text)
        db.run("INSERT INTO handoff_msgs (admin_message_id, handoff_id) VALUES (?,?)", (sent.message_id, handoff_id))

    async def deliver(bot: Bot, message: Message, result: Answer) -> None:
        text = result.text
        if result.sources:
            text += "\n\n📚 " + " · ".join(result.sources)
        markup = None if result.limited else feedback_keyboard()
        await message.answer(text, reply_markup=markup)

        for event in result.events:
            if event["type"] != "handoff":
                continue
            handoff = db.one("SELECT * FROM handoffs WHERE id=?", (event["id"],))
            if not settings.admin_chat_id:
                db.run("UPDATE handoffs SET status='closed' WHERE id=?", (handoff["id"],))
                await message.answer("(Demo note: no human is connected to this demo, so I'll keep helping myself.)")
                continue
            history = "\n".join(f"{'Customer' if m['role'] == 'user' else 'Bot'}: {m['content'][:200]}" for m in db.history(handoff["chat_id"], 3))
            await to_admin(
                bot, handoff["id"],
                f"🙋 Handoff #{handoff['id']} from {handoff['name'] or 'customer'}\n\n{handoff['summary']}\n\n{history}\n\n"
                "Reply to this message to answer the customer. /close " + str(handoff["id"]) + " ends it.",
            )

    async def handle_text(bot: Bot, message: Message, text: str) -> None:
        chat_id = message.chat.id
        name = message.from_user.first_name if message.from_user else ""
        handoff = open_handoff(chat_id)
        if handoff:
            await to_admin(bot, handoff["id"], f"💬 #{handoff['id']} {name}: {text}")
            db.add_message(chat_id, "user", text)
            return
        async with locks[chat_id]:
            await bot.send_chat_action(chat_id, "typing")
            try:
                result = await agent.answer(chat_id, name, text)
            except Exception:
                import logging

                logging.getLogger("support-bot").exception("answer failed")
                await message.answer("Sorry, something went wrong on my side. Please try again in a moment, or type /human.")
                return
            await deliver(bot, message, result)

    # ---------------------------------------------------------------- admin
    @router.message(Command("close"), F.func(lambda m: is_admin(m.chat.id)))
    async def close_cmd(message: Message, command: CommandObject, bot: Bot):
        if not command.args or not command.args.strip().isdigit():
            await message.answer("Usage: /close <handoff number>")
            return
        handoff = db.one("SELECT * FROM handoffs WHERE id=?", (int(command.args),))
        if not handoff:
            await message.answer("No such handoff.")
            return
        db.run("UPDATE handoffs SET status='closed' WHERE id=?", (handoff["id"],))
        await bot.send_message(handoff["chat_id"], "Our team closed this conversation. I'm back if you need anything else! 🙂")
        await message.answer(f"Handoff #{handoff['id']} closed.")

    @router.message(Command("stats"), F.func(lambda m: is_admin(m.chat.id)))
    async def stats_cmd(message: Message):
        row = db.one("SELECT COALESCE(SUM(messages),0) m, COUNT(*) u FROM usage WHERE day=date('now')") or {}
        open_n = db.one("SELECT COUNT(*) n FROM handoffs WHERE status='open' AND chat_id > 0")["n"]
        await message.answer(
            f"Today: {row.get('m', 0)} messages from {row.get('u', 0)} people\n"
            f"Spend: ${db.spend_today():.4f} of ${settings.global_daily_budget_usd:.2f}\nOpen handoffs: {open_n}"
        )

    @router.message(F.reply_to_message, F.func(lambda m: is_admin(m.chat.id)))
    async def admin_reply(message: Message, bot: Bot):
        link = db.one("SELECT handoff_id FROM handoff_msgs WHERE admin_message_id=?", (message.reply_to_message.message_id,))
        if not link or not message.text:
            return
        handoff = db.one("SELECT * FROM handoffs WHERE id=?", (link["handoff_id"],))
        await bot.send_message(handoff["chat_id"], f"👩‍💼 Support team:\n{message.text}")
        db.add_message(handoff["chat_id"], "assistant", f"[human] {message.text}")

    # ----------------------------------------------------------------- user
    @router.message(Command("start"))
    async def start_cmd(message: Message):
        name = message.from_user.first_name if message.from_user else "there"
        await message.answer(WELCOME.format(name=name, shop=settings.shop_name), reply_markup=quick_keyboard())

    @router.message(Command("help"))
    async def help_cmd(message: Message):
        await message.answer(HELP)

    @router.message(Command("id"))
    async def id_cmd(message: Message):
        await message.answer(f"Your chat id is {message.chat.id}")

    @router.message(Command("reset"))
    async def reset_cmd(message: Message):
        db.forget(message.chat.id)
        await message.answer("Done, I forgot our conversation. 🧹")

    @router.message(Command("human"))
    async def human_cmd(message: Message, bot: Bot):
        await handle_text(bot, message, "I would like to talk to a human agent, please.")

    @router.callback_query(F.data.startswith("q:"))
    async def quick(call: CallbackQuery, bot: Bot):
        await call.answer()
        key = call.data[2:]
        text = "I would like to talk to a human agent, please." if key == "human" else QUICK.get(key)
        if text and call.message:
            await call.message.answer(f"“{text}”")
            await handle_text(bot, call.message.model_copy(update={"from_user": call.from_user}), text)

    @router.callback_query(F.data.startswith("fb:"))
    async def feedback(call: CallbackQuery):
        db.run("INSERT INTO feedback (chat_id, helpful, created_at) VALUES (?,?,?)",
               (call.message.chat.id, int(call.data[3:]), now()))
        await call.answer("Thanks for the feedback!")
        if call.message:
            await call.message.edit_reply_markup(reply_markup=None)

    @router.message(F.text)
    async def text_message(message: Message, bot: Bot):
        if not message.from_user or not throttle.allow(message.from_user.id):
            return
        await handle_text(bot, message, message.text)

    @router.message()
    async def other(message: Message):
        await message.answer("I can only read text messages. Please type your question.")

    dp = Dispatcher()
    dp.include_router(router)
    return dp


COMMANDS = [
    BotCommand(command="start", description="Start"),
    BotCommand(command="human", description="Talk to a person"),
    BotCommand(command="reset", description="Forget our conversation"),
    BotCommand(command="help", description="What can I ask?"),
]
