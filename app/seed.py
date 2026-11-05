"""Demo catalogue and orders for the fictional shop. Everything here is made up."""
import json
from datetime import date, timedelta

from .db import Db

PRODUCTS = [
    ("NG-BUDS-01", "Pulse Buds", "Audio", 59.0, 120, "True wireless earbuds with active noise cancelling and 28 hours of battery with the case."),
    ("NG-BUDS-02", "Pulse Buds Pro", "Audio", 99.0, 40, "Premium earbuds with adaptive noise cancelling, spatial audio and wireless charging."),
    ("NG-SPK-01", "Echo Mini", "Audio", 45.0, 75, "Pocket Bluetooth speaker, IP67 waterproof, 12 hours of playtime."),
    ("NG-SPK-02", "Echo Max", "Audio", 129.0, 0, "Room-filling Bluetooth speaker with 24 hours of playtime. Currently out of stock."),
    ("NG-WATCH-01", "Orbit Watch", "Wearables", 149.0, 33, "Fitness smartwatch with GPS, heart-rate tracking and a 10-day battery."),
    ("NG-BAND-01", "Orbit Band", "Wearables", 39.0, 210, "Slim activity tracker with sleep tracking and a 14-day battery."),
    ("NG-CHG-01", "Volt 65W Charger", "Power", 35.0, 300, "Compact 65W GaN charger with two USB-C ports and one USB-A port."),
    ("NG-PWR-01", "Volt Bank 20K", "Power", 49.0, 88, "20,000 mAh power bank with 45W fast charging, airline friendly."),
    ("NG-CBL-01", "Braided USB-C Cable 2m", "Power", 14.0, 500, "Durable braided USB-C to USB-C cable, 100W rated."),
    ("NG-CASE-01", "Shell Case for Orbit Watch", "Accessories", 19.0, 60, "Protective bumper case for the Orbit Watch."),
]


def _order(number, email, status, placed_days_ago, items, carrier="", tracking="", eta_days=None):
    total = round(sum(price * qty for _, price, qty in items), 2)
    eta = (date.today() + timedelta(days=eta_days)).isoformat() if eta_days is not None else ""
    placed = (date.today() - timedelta(days=placed_days_ago)).isoformat()
    lines = [{"name": name, "price": price, "qty": qty} for name, price, qty in items]
    return (number, email, status, carrier, tracking, eta, total, placed, json.dumps(lines))


ORDERS = [
    _order("NG-10001", "alex@example.com", "shipped", 3, [("Pulse Buds", 59.0, 1)], "DHL", "JD0146200815", 2),
    _order("NG-10002", "sam@example.com", "processing", 1, [("Orbit Watch", 149.0, 1), ("Shell Case for Orbit Watch", 19.0, 1)], eta_days=5),
    _order("NG-10003", "jo@example.com", "delivered", 9, [("Volt 65W Charger", 35.0, 2)], "UPS", "1Z999AA10123456784", -6),
    _order("NG-10004", "alex@example.com", "delivered", 40, [("Echo Mini", 45.0, 1)], "DHL", "JD0146200777", -33),
    _order("NG-10005", "mia@example.com", "cancelled", 6, [("Echo Max", 129.0, 1)]),
]


def seed_demo(db: Db) -> None:
    """Resets the catalogue and orders to a known state (the public demo does this daily)."""
    db.run("DELETE FROM products")
    db.run("DELETE FROM orders")
    db.run("DELETE FROM returns")
    for p in PRODUCTS:
        db.run("INSERT INTO products VALUES (?,?,?,?,?,?)", p)
    for o in ORDERS:
        db.run("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", o)
