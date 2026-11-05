"""Admin area: a real login page, a signed session cookie and the dashboard pages.

Two roles: "admin" sees everything, "demo" is read-only and sees only the sample conversations, so
portfolio visitors can explore the dashboard without reading real visitors' messages.
"""
import hashlib
import hmac
import math
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .config import Settings
from .db import Db
from .samples import DEMO_CHATS, SAMPLE_CHATS

COOKIE = "support_session"
SESSION_SECONDS = 12 * 3600

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def ago(ts: str) -> str:
    try:
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


templates.env.filters["ago"] = ago


# ----------------------------------------------------------------------- session
def make_token(settings: Settings, role: str) -> str:
    body = f"{role}.{int(time.time()) + SESSION_SECONDS}"
    sig = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(settings: Settings, token: str | None) -> str | None:
    try:
        role, exp, sig = (token or "").split(".")
        good = hmac.new(settings.session_secret.encode(), f"{role}.{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time() and role in ("admin", "demo"):
            return role
    except ValueError:
        pass
    return None


class LoginLimiter:
    """Eight wrong passwords from one address lock it out for ten minutes."""

    def __init__(self, tries: int = 8, window: int = 600):
        self.tries, self.window, self.fails = tries, window, defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q, now = self.fails[ip], time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.tries

    def fail(self, ip: str) -> None:
        self.fails[ip].append(time.monotonic())


# ------------------------------------------------------------------------ charts
def nice_max(v: float) -> float:
    if v <= 0:
        return 1
    base = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * base:
            return m * base
    return 10 * base


def _grid(mx: float, top: int, ih: int, left: int, w: int, right: int, fmt) -> str:
    out = []
    for k in range(5):
        y = top + ih * k / 4
        out.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        out.append(f'<text class="axis" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{fmt(mx * (4 - k) / 4)}</text>')
    return "".join(out)


def bar_chart(values: list[float], labels: list[str], fmt=lambda v: f"{v:g}", uid: str = "b", h: int = 300) -> Markup:
    w, left, right, top, bottom = 640, 40, 6, 12, 26
    iw, ih = w - left - right, h - top - bottom
    mx = nice_max(max(values))
    step = iw / len(values)
    bw = step * 0.56
    parts = [
        f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">',
        f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--accent2)"/><stop offset="1" style="stop-color:var(--accent)"/></linearGradient></defs>',
        _grid(mx, top, ih, left, w, right, lambda v: f"{v:g}"),
    ]
    for i, v in enumerate(values):
        bh = ih * v / mx
        x = left + i * step + (step - bw) / 2
        cls = "bar now" if i == len(values) - 1 else "bar"
        parts.append(
            f'<rect class="{cls}" x="{x:.1f}" y="{top + ih - bh:.1f}" width="{bw:.1f}" height="{max(bh, 1.5):.1f}" rx="5" fill="url(#{uid})">'
            f"<title>{labels[i]}: {fmt(v)}</title></rect>"
        )
        if i % 2 == (len(values) - 1) % 2:
            parts.append(f'<text class="axis" x="{x + bw / 2:.1f}" y="{h - 7}" text-anchor="middle">{labels[i]}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def area_chart(values: list[float], labels: list[str], fmt=lambda v: f"{v:g}", uid: str = "a") -> Markup:
    w, h, left, right, top, bottom = 640, 240, 46, 26, 14, 26
    iw, ih = w - left - right, h - top - bottom
    mx = nice_max(max(values))
    n = len(values)
    pts = [(left + iw * i / (n - 1), top + ih - ih * v / mx) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"M{pts[0][0]:.1f},{top + ih} L" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{top + ih} Z"
    parts = [
        f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">',
        f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--accent);stop-opacity:.38"/><stop offset="1" style="stop-color:var(--accent);stop-opacity:0"/></linearGradient></defs>',
        _grid(mx, top, ih, left, w, right, fmt),
        f'<path d="{area}" fill="url(#{uid})"/>',
        f'<polyline class="line" points="{line}" fill="none"/>',
    ]
    for i, (x, y) in enumerate(pts):
        parts.append(f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="{4.5 if i == n - 1 else 2.5}"><title>{labels[i]}: {fmt(values[i])}</title></circle>')
        if i % 2 == (n - 1) % 2:
            parts.append(f'<text class="axis" x="{x:.1f}" y="{h - 7}" text-anchor="middle">{labels[i]}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def spark(values: list[float]) -> Markup:
    mx, mn = max(values), min(values)
    span = (mx - mn) or 1
    n = len(values)
    pts = " ".join(f"{100 * i / (n - 1):.1f},{26 - 22 * (v - mn) / span:.1f}" for i, v in enumerate(values))
    return Markup(f'<svg viewBox="0 0 100 30" class="spark" preserveAspectRatio="none"><polyline points="{pts}" fill="none"/></svg>')


def donut(pct: float) -> Markup:
    c = 2 * math.pi * 26
    return Markup(
        '<svg viewBox="0 0 64 64" class="donut"><circle class="track" cx="32" cy="32" r="26"/>'
        f'<circle class="fill" cx="32" cy="32" r="26" stroke-dasharray="{c * pct / 100:.1f} {c:.1f}" transform="rotate(-90 32 32)"/></svg>'
    )


# -------------------------------------------------------------------------- data
def series(db: Db, days: int = 14) -> list[dict]:
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = {
        r["day"]: r
        for r in db.all(
            "SELECT day, SUM(messages) m, COUNT(CASE WHEN messages > 0 THEN 1 END) p, SUM(prompt_tokens) pt, "
            "SUM(completion_tokens) ct, SUM(cost) c FROM usage WHERE day >= ? GROUP BY day",
            (start,),
        )
    }
    out = []
    for i in range(days):
        d = date.today() - timedelta(days=days - 1 - i)
        r = rows.get(d.isoformat()) or {}
        out.append(
            {
                "label": d.strftime("%d %b"),
                "m": int(r.get("m") or 0),
                "p": int(r.get("p") or 0),
                "tokens": int((r.get("pt") or 0) + (r.get("ct") or 0)),
                "cost": float(r.get("c") or 0),
            }
        )
    return out


def chat_name(chat_id: int) -> str:
    if chat_id in SAMPLE_CHATS:
        return SAMPLE_CHATS[chat_id]["name"]
    return f"Visitor ••{str(abs(chat_id))[-4:]}"


def visible(role: str) -> str:
    return DEMO_CHATS if role == "demo" else "1=1"


def conversations(db: Db, role: str, limit: int = 40) -> list[dict]:
    rows = db.all(
        "SELECT chat_id, COUNT(*) n, MAX(id) last_id, MAX(created_at) last FROM messages "
        f"WHERE role != 'system' AND {visible(role)} GROUP BY chat_id ORDER BY MAX(created_at) DESC, last_id DESC LIMIT ?",
        (limit,),
    )
    for r in rows:
        first = db.one("SELECT content FROM messages WHERE chat_id=? AND role='user' ORDER BY id LIMIT 1", (r["chat_id"],))
        r["name"] = chat_name(r["chat_id"])
        r["sample"] = r["chat_id"] in SAMPLE_CHATS
        r["preview"] = (first["content"] if first else "")[:90]
        r["handoff"] = bool(db.one("SELECT 1 x FROM handoffs WHERE chat_id=?", (r["chat_id"],)))
    return rows


def transcript(db: Db, chat_id: int) -> list[dict]:
    out = []
    for m in db.all("SELECT role, content, created_at FROM messages WHERE chat_id=? AND role != 'system' ORDER BY id LIMIT 200", (chat_id,)):
        if m["role"] == "user":
            who, text = "customer", m["content"]
        elif m["content"].startswith("[human] "):
            who, text = "agent", m["content"][8:]
        else:
            who, text = "bot", m["content"]
        out.append({"who": who, "text": text, "at": m["created_at"][11:16]})
    return out


def handoffs(db: Db, role: str, limit: int = 50) -> list[dict]:
    return db.all(f"SELECT * FROM handoffs WHERE {visible(role)} ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter()
    limiter = LoginLimiter()

    def db_of(request: Request) -> Db:
        return request.app.state.s["db"]

    def role_of(request: Request) -> str | None:
        return read_token(settings, request.cookies.get(COOKIE))

    def page(request: Request, name: str, role: str, **ctx):
        ctx |= {
            "role": role,
            "role_label": "Administrator" if role == "admin" else "Demo viewer",
            "shop": settings.shop_name,
            "bot": settings.telegram_bot_username,
        }
        return templates.TemplateResponse(request, name, ctx)

    def login_page(request: Request, error: str = "", status: int = 200):
        response = templates.TemplateResponse(
            request,
            "login.html",
            {"error": error, "shop": settings.shop_name, "demo_user": settings.demo_user, "demo_password": settings.demo_password if settings.demo_user else ""},
        )
        response.status_code = status
        return response

    @router.get("/admin/login")
    async def login_form(request: Request):
        if role_of(request):
            return RedirectResponse("/admin", 303)
        return login_page(request)

    @router.post("/admin/login")
    async def login(request: Request):
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
        if limiter.blocked(ip):
            return login_page(request, "Too many attempts. Try again in a few minutes.", 429)
        form = await request.form()
        user, password = str(form.get("username", "")).strip(), str(form.get("password", ""))
        role = None
        if hmac.compare_digest(user, settings.admin_user) & hmac.compare_digest(password, settings.admin_password):
            role = "admin"
        elif settings.demo_user and hmac.compare_digest(user, settings.demo_user) & hmac.compare_digest(password, settings.demo_password):
            role = "demo"
        if not role:
            limiter.fail(ip)
            return login_page(request, "That username and password do not match.", 401)
        response = RedirectResponse("/admin", 303)
        response.set_cookie(
            COOKIE, make_token(settings, role), max_age=SESSION_SECONDS, httponly=True, samesite="lax",
            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        )
        return response

    @router.post("/admin/logout")
    async def logout():
        response = RedirectResponse("/admin/login", 303)
        response.delete_cookie(COOKIE)
        return response

    @router.get("/admin")
    async def overview(request: Request):
        role = role_of(request)
        if not role:
            return RedirectResponse("/admin/login", 303)
        db = db_of(request)
        days = series(db)
        labels = [d["label"] for d in days]
        today = days[-1]
        spend = db.spend_today()
        budget = settings.global_daily_budget_usd
        fb = db.one("SELECT COALESCE(SUM(helpful),0) up, COUNT(*) n FROM feedback") or {"up": 0, "n": 0}
        helpful = round(fb["up"] / fb["n"] * 100) if fb["n"] else 0
        total_msgs = sum(d["m"] for d in days) or 1
        convs = conversations(db, role, 5)
        hand = handoffs(db, role, 4)
        return page(
            request, "overview.html", role, nav="overview",
            kpi={
                "messages": today["m"], "people": today["p"], "spend": spend, "budget": budget,
                "budget_pct": min(100, round(spend / budget * 100)) if budget else 0,
                "helpful": helpful, "votes": fb["n"],
                "tokens_today": today["tokens"], "avg_cost": sum(d["cost"] for d in days) / total_msgs,
                "month_cost": sum(d["cost"] for d in days),
                "msgs_14": sum(d["m"] for d in days),
            },
            sparks={"m": spark([d["m"] for d in days]), "p": spark([d["p"] for d in days]), "c": spark([d["cost"] for d in days])},
            donut=donut(helpful),
            chart_msgs=bar_chart([d["m"] for d in days], labels, uid="gm"),
            chart_cost=area_chart([d["cost"] for d in days], labels, fmt=lambda v: f"${v:.3f}", uid="gc"),
            chart_tokens=area_chart([d["tokens"] / 1000 for d in days], labels, fmt=lambda v: f"{v:g}k", uid="gt"),
            convs=convs, handoffs=hand,
            open_handoffs=sum(1 for h in handoffs(db, role) if h["status"] == "open"),
            limits={
                "user_daily_limit": settings.user_daily_limit, "max_output_tokens": settings.max_output_tokens,
                "history_turns": settings.history_turns, "model": settings.chat_model, "embed_model": settings.embed_model,
                "ai_on": bool(settings.openai_api_key),
            },
        )

    @router.get("/admin/conversations")
    async def conversations_page(request: Request, chat: int | None = None):
        role = role_of(request)
        if not role:
            return RedirectResponse("/admin/login", 303)
        db = db_of(request)
        convs = conversations(db, role)
        current = next((c for c in convs if c["chat_id"] == chat), convs[0] if convs else None)
        return page(
            request, "conversations.html", role, nav="conversations", convs=convs, current=current,
            messages=transcript(db, current["chat_id"]) if current else [],
        )

    @router.get("/admin/handoffs")
    async def handoffs_page(request: Request):
        role = role_of(request)
        if not role:
            return RedirectResponse("/admin/login", 303)
        items = handoffs(db_of(request), role)
        return page(request, "handoffs.html", role, nav="handoffs", items=items, open_count=sum(1 for h in items if h["status"] == "open"))

    @router.get("/admin/knowledge")
    async def knowledge_page(request: Request):
        role = role_of(request)
        if not role:
            return RedirectResponse("/admin/login", 303)
        rows = db_of(request).all("SELECT id, source, title, body, embedding IS NOT NULL AS emb FROM kb_chunks ORDER BY source, id")
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["source"], []).append(r)
        return page(
            request, "knowledge.html", role, nav="knowledge", groups=groups, total=len(rows),
            embedded=sum(1 for r in rows if r["emb"]),
        )

    return router
