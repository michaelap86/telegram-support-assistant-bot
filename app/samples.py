"""Made-up history for the public demo, so the dashboard has something to show from day one.

Sample rows use small negative chat ids (-1 to -1999). Real Telegram users have positive ids, and the
read-only demo login of the dashboard can only see the sample conversations, never real visitors.
"""
import random
from datetime import date, datetime, timedelta, timezone

from .db import Db

SAMPLE_RANGE = "chat_id BETWEEN -1999 AND -1"
DEMO_CHATS = "chat_id BETWEEN -9 AND -1"

SAMPLE_CHATS = {
    -1: {
        "name": "Maya R.",
        "minutes_ago": 14,
        "turns": [
            ("user", "Where is my order NG-10001? alex@example.com"),
            ("assistant", "Good news: order NG-10001 has shipped with DHL and is on its way. It includes 1 x Pulse Buds. Tracking number: JD0146200815, estimated delivery in two days."),
            ("user", "Great, and can I change the delivery address?"),
            ("assistant", "Once a parcel has shipped the address can no longer be changed, but you can redirect it on the DHL website with the tracking number JD0146200815."),
        ],
    },
    -2: {
        "name": "Tom B.",
        "minutes_ago": 55,
        "turns": [
            ("user", "Do you have earbuds under 70 euro? Mostly for the gym."),
            ("assistant", "Yes! The Pulse Buds are 59 EUR: true wireless, active noise cancelling and 28 hours of battery with the case. They are sweat and splash resistant, so they work well for the gym."),
            ("user", "And a waterproof speaker?"),
            ("assistant", "The Echo Mini is 45 EUR, IP67 waterproof, with 12 hours of playtime. It is in stock. The bigger Echo Max is currently sold out."),
        ],
    },
    -3: {
        "name": "Lena K.",
        "minutes_ago": 130,
        "turns": [
            ("user", "I want to return my chargers, order NG-10003, jo@example.com. Wrong plug type."),
            ("assistant", "Done. Your return RT-0001 is registered. A prepaid label is on its way to your email within one business day, and your refund is issued after the parcel is inspected (usually 2 business days)."),
        ],
    },
    -4: {
        "name": "Jonas W.",
        "minutes_ago": 240,
        "turns": [
            ("user", "Wie lange dauert der Versand nach Österreich und was kostet er?"),
            ("assistant", "Standardversand innerhalb der EU dauert 3 bis 5 Werktage und kostet 4,90 EUR. Express (1 bis 2 Werktage) kostet 9,90 EUR. Ab einem Bestellwert von 60 EUR ist der Standardversand kostenlos."),
        ],
    },
    -5: {
        "name": "Chris D.",
        "minutes_ago": 410,
        "turns": [
            ("user", "my left earbud stopped charging, order NG-10006"),
            ("assistant", "I'm sorry about that. A charging problem is covered by the 2 year warranty, so I've passed your case to a human agent who will arrange a repair or replacement."),
            ("assistant", "[human] Hi Chris, this is Anna from the support team. Sorry for the trouble! I've sent a prepaid repair label to your email. Repairs take 7 to 10 business days."),
            ("user", "Thank you, that was quick!"),
        ],
    },
    -6: {
        "name": "Sofía M.",
        "minutes_ago": 9,
        "turns": [
            ("user", "Necesito una factura con el CIF de mi empresa para el pedido NG-10002."),
            ("assistant", "Con gusto. Las facturas con datos de empresa las prepara nuestro equipo, así que he pasado tu solicitud a una persona. Te responderá aquí en breve."),
        ],
    },
}

SAMPLE_HANDOFFS = [
    (-5, "Chris D.", "Left earbud stopped charging (order NG-10006). Wants a repair or replacement.", "closed", 405),
    (-6, "Sofía M.", "Needs an invoice with a company tax number for order NG-10002.", "open", 8),
]


def _ts(minutes_ago: float = 0, days_ago: int = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago, days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def seed_samples(db: Db) -> None:
    """(Re)creates the sample conversations, handoffs, feedback and 13 days of usage history."""
    for table in ("messages", "handoffs", "feedback", "usage"):
        db.run(f"DELETE FROM {table} WHERE {SAMPLE_RANGE}")

    for chat_id, chat in SAMPLE_CHATS.items():
        for i, (role, content) in enumerate(chat["turns"]):
            when = _ts(minutes_ago=chat["minutes_ago"] - i * 0.4)
            db.run("INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)", (chat_id, role, content, when))

    for chat_id, name, summary, status, minutes in SAMPLE_HANDOFFS:
        db.run(
            "INSERT INTO handoffs (chat_id, name, summary, status, created_at) VALUES (?,?,?,?,?)",
            (chat_id, name, summary, status, _ts(minutes_ago=minutes)),
        )

    rng = random.Random(7)
    for days_ago in range(1, 14):
        weekday = (date.today() - timedelta(days=days_ago)).weekday()
        people = rng.randint(12, 22) + (0 if weekday >= 5 else rng.randint(6, 14))
        counts = [1] * people
        for _ in range(int(people * rng.uniform(1.4, 2.4))):
            counts[rng.randrange(people)] += 1
        for i, msgs in enumerate(counts):
            prompt = msgs * rng.randint(1100, 1700)
            completion = msgs * rng.randint(60, 120)
            cost = (prompt * 0.15 + completion * 0.60) / 1_000_000
            db.run(
                "INSERT INTO usage (day, chat_id, messages, prompt_tokens, completion_tokens, cost) VALUES (?,?,?,?,?,?)",
                ((date.today() - timedelta(days=days_ago)).isoformat(), -100 - i, msgs, prompt, completion, cost),
            )

    votes = [1] * 42 + [0] * 4
    rng.shuffle(votes)
    for i, helpful in enumerate(votes):
        db.run(
            "INSERT INTO feedback (chat_id, helpful, created_at) VALUES (?,?,?)",
            (-1000 - i, helpful, _ts(minutes_ago=rng.randint(0, 1400), days_ago=rng.randint(0, 12))),
        )
