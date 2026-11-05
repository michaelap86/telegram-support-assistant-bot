"""Actions the assistant can take. Each one checks its own inputs: the model proposes, the code decides."""
import json
from datetime import date, datetime

from .db import Db, now
from .kb import tokens

RETURN_WINDOW_DAYS = 30

SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "check_order",
            "description": "Look up the status of an order. Needs BOTH the order number (like NG-10001) and the email used for the order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order number, e.g. NG-10001"},
                    "email": {"type": "string", "description": "Email address on the order"},
                },
                "required": ["order_id", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_products",
            "description": "Search the product catalogue for price and stock. Use for questions like 'do you have X', 'how much is Y', 'what do you have under 50 euro'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What the customer is looking for, e.g. 'earbuds' or 'charger'"},
                    "max_price": {"type": "number", "description": "Optional price ceiling in EUR"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_return",
            "description": "Register a return for a delivered order. Needs the order number, the email on the order and the reason. Only call it after the customer clearly asked to return something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "email": {"type": "string"},
                    "reason": {"type": "string", "description": "Short reason in the customer's words"},
                },
                "required": ["order_id", "email", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "talk_to_human",
            "description": "Hand the conversation to a human agent. Use when the customer asks for a person, is upset, reports a defect or warranty claim, or the answer is not in the knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "One or two sentences so the agent knows the situation"}},
                "required": ["summary"],
            },
        },
    },
]


class Tools:
    def __init__(self, db: Db):
        self.db = db

    def run(self, name: str, args: dict, ctx: dict) -> str:
        handler = getattr(self, f"_{name}", None)
        if not handler:
            return json.dumps({"error": f"Unknown tool {name}"})
        try:
            return json.dumps(handler(ctx=ctx, **args), ensure_ascii=False)
        except TypeError:
            return json.dumps({"error": "Missing or invalid arguments"})

    # An order is only shown when the email matches, so one customer cannot read another one's order.
    def _verified_order(self, order_id: str, email: str) -> dict | None:
        order = self.db.order(order_id)
        if order and order["email"].lower() == email.strip().lower():
            return order
        return None

    def _check_order(self, order_id: str, email: str, ctx: dict) -> dict:
        order = self._verified_order(order_id, email)
        if not order:
            return {"found": False, "message": "No order matches that number and email. Ask the customer to double-check both."}
        out = {
            "found": True,
            "order_id": order["id"],
            "status": order["status"],
            "placed_at": order["placed_at"],
            "total_eur": order["total"],
            "items": [f"{i['qty']} x {i['name']}" for i in order["items"]],
        }
        if order["carrier"]:
            out["carrier"] = order["carrier"]
            out["tracking_number"] = order["tracking"]
        if order["eta"]:
            out["delivery_date" if order["status"] == "delivered" else "estimated_delivery"] = order["eta"]
        return out

    def _find_products(self, query: str, ctx: dict, max_price: float | None = None) -> dict:
        wanted = set(tokens(query))
        rows = self.db.all("SELECT * FROM products")
        hits = []
        for p in rows:
            words = set(tokens(f"{p['name']} {p['category']} {p['description']}"))
            if wanted & words and (max_price is None or p["price"] <= max_price):
                hits.append(p)
        hits.sort(key=lambda p: p["price"])
        return {
            "products": [
                {
                    "name": p["name"],
                    "price_eur": p["price"],
                    "in_stock": p["stock"] > 0,
                    "stock_note": "low stock" if 0 < p["stock"] < 20 else "",
                    "about": p["description"],
                }
                for p in hits[:5]
            ]
        }

    def _start_return(self, order_id: str, email: str, reason: str, ctx: dict) -> dict:
        order = self._verified_order(order_id, email)
        if not order:
            return {"ok": False, "message": "No order matches that number and email."}
        if order["status"] != "delivered":
            return {"ok": False, "message": f"The order is '{order['status']}', so it cannot be returned yet. Only delivered orders can be returned."}
        delivered = datetime.strptime(order["eta"], "%Y-%m-%d").date() if order["eta"] else date.today()
        days = (date.today() - delivered).days
        if days > RETURN_WINDOW_DAYS:
            return {"ok": False, "message": f"The 30 day return window ended {days - RETURN_WINDOW_DAYS} days ago."}
        if self.db.one("SELECT id FROM returns WHERE order_id=?", (order["id"],)):
            return {"ok": False, "message": "A return for this order is already registered."}
        rid = self.db.run(
            "INSERT INTO returns (order_id, reason, created_at) VALUES (?,?,?)",
            (order["id"], reason[:300], now()),
        )
        return {"ok": True, "return_id": f"RT-{rid:04d}", "message": "Return registered. A prepaid label is emailed within one business day."}

    def _talk_to_human(self, summary: str, ctx: dict) -> dict:
        existing = self.db.one("SELECT id FROM handoffs WHERE chat_id=? AND status='open'", (ctx["chat_id"],))
        if existing:
            return {"ok": True, "message": "A human agent already has this conversation."}
        hid = self.db.run(
            "INSERT INTO handoffs (chat_id, name, summary, created_at) VALUES (?,?,?,?)",
            (ctx["chat_id"], ctx.get("name", ""), summary[:500], now()),
        )
        ctx.setdefault("events", []).append({"type": "handoff", "id": hid})
        return {"ok": True, "message": "A human agent has been notified and will reply here. Typical wait during working hours is a few minutes."}
