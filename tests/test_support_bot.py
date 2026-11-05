import json

import pytest
from fastapi.testclient import TestClient

from app.llm import Reply, ToolCall
from app.tools import Tools
from app.web import create_app

from .conftest import ScriptedLLM, say


# ------------------------------------------------------------ knowledge base
async def test_search_finds_the_right_section(make_agent):
    _, kb = await make_agent()
    top = await kb.search("how long until my refund arrives?")
    assert top[0]["title"] == "Refund timing"
    top = await kb.search("how do I pair my Pulse Buds")
    assert top[0]["title"] == "Pairing Pulse Buds"


async def test_search_returns_nothing_for_unrelated_questions(make_agent):
    _, kb = await make_agent()
    assert await kb.search("who won the football match yesterday") == []


async def test_sync_is_idempotent_and_picks_up_changes(make_agent, db, tmp_path, settings):
    _, kb = await make_agent()
    first = db.all("SELECT id, hash FROM kb_chunks")
    await kb.sync()
    assert db.all("SELECT id, hash FROM kb_chunks") == first

    (tmp_path / "x.md").write_text("# X\n\n## Gift wrap\nWe wrap gifts for free.\n")
    kb.directory = str(tmp_path)
    await kb.sync()
    assert [r["title"] for r in db.all("SELECT title FROM kb_chunks")] == ["Gift wrap"]


# --------------------------------------------------------------------- tools
def test_order_needs_the_matching_email(db):
    tools = Tools(db)
    wrong = json.loads(tools.run("check_order", {"order_id": "NG-10001", "email": "eve@example.com"}, {}))
    assert wrong["found"] is False and "status" not in wrong

    right = json.loads(tools.run("check_order", {"order_id": "ng-10001", "email": "ALEX@example.com"}, {}))
    assert right["status"] == "shipped" and right["carrier"] == "DHL"


def test_return_rules(db):
    tools = Tools(db)
    ok = json.loads(tools.run("start_return", {"order_id": "NG-10003", "email": "jo@example.com", "reason": "wrong plug"}, {}))
    assert ok["ok"] and ok["return_id"].startswith("RT-")
    again = json.loads(tools.run("start_return", {"order_id": "NG-10003", "email": "jo@example.com", "reason": "x"}, {}))
    assert not again["ok"] and "already" in again["message"]

    shipped = json.loads(tools.run("start_return", {"order_id": "NG-10001", "email": "alex@example.com", "reason": "x"}, {}))
    assert not shipped["ok"] and "shipped" in shipped["message"]

    old = json.loads(tools.run("start_return", {"order_id": "NG-10004", "email": "alex@example.com", "reason": "x"}, {}))
    assert not old["ok"] and "window" in old["message"]

    stranger = json.loads(tools.run("start_return", {"order_id": "NG-10003", "email": "eve@example.com", "reason": "x"}, {}))
    assert not stranger["ok"]


def test_product_search_and_price_ceiling(db):
    tools = Tools(db)
    found = json.loads(tools.run("find_products", {"query": "earbuds", "max_price": 70}, {}))["products"]
    assert [p["name"] for p in found] == ["Pulse Buds"]
    speaker = json.loads(tools.run("find_products", {"query": "speaker"}, {}))["products"]
    assert {p["name"]: p["in_stock"] for p in speaker} == {"Echo Mini": True, "Echo Max": False}


def test_handoff_is_created_once(db):
    tools, ctx = Tools(db), {"chat_id": 7, "name": "Ana"}
    tools.run("talk_to_human", {"summary": "Cracked screen"}, ctx)
    tools.run("talk_to_human", {"summary": "Cracked screen"}, ctx)
    assert db.one("SELECT COUNT(*) n FROM handoffs")["n"] == 1
    assert len(ctx["events"]) == 1


def test_bad_tool_arguments_do_not_crash(db):
    assert "error" in json.loads(Tools(db).run("check_order", {"order_id": "NG-10001"}, {}))
    assert "error" in json.loads(Tools(db).run("drop_database", {}, {}))


# --------------------------------------------------------------------- agent
async def test_agent_uses_a_tool_then_answers(make_agent, db):
    llm = ScriptedLLM(
        [
            Reply(tool_calls=[ToolCall("c1", "check_order", {"order_id": "NG-10001", "email": "alex@example.com"})], prompt_tokens=500, completion_tokens=30),
            say("Your order is on its way with DHL.", prompt=600, completion=25),
        ]
    )
    agent, _ = await make_agent(llm)
    out = await agent.answer(1, "Alex", "where is NG-10001? alex@example.com")

    assert out.text == "Your order is on its way with DHL."
    assert out.sources == []  # answers built from private order data do not cite articles
    tool_message = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "JD0146200815" in tool_message["content"]
    assert out.cost == pytest.approx((1100 * 0.15 + 55 * 0.60) / 1_000_000)
    assert db.user_messages_today(1) == 1  # one customer message, even though the model was called twice


async def test_agent_passes_policy_excerpts_and_trimmed_input(make_agent):
    llm = ScriptedLLM([say("30 days.")])
    agent, _ = await make_agent(llm)
    out = await agent.answer(1, "Ana", "what is the return window? " + "x" * 5000)

    system = llm.calls[0]["messages"][0]["content"]
    assert "Return window" in system and "30 days" in system
    assert len(llm.calls[0]["messages"][-1]["content"]) <= 600
    assert llm.calls[0]["max_tokens"] == 350
    assert "Return window" in out.sources


async def test_conversation_memory_is_sent_back(make_agent):
    llm = ScriptedLLM([say("Hi!"), say("Sure.")])
    agent, _ = await make_agent(llm)
    await agent.answer(1, "Ana", "hello")
    await agent.answer(1, "Ana", "and shipping?")
    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]


async def test_daily_user_limit_blocks_before_calling_the_model(make_agent, settings):
    llm = ScriptedLLM([say("ok")] * 5)
    agent, _ = await make_agent(llm)
    settings.user_daily_limit = 2
    await agent.answer(1, "Ana", "one")
    await agent.answer(1, "Ana", "two")
    blocked = await agent.answer(1, "Ana", "three")
    assert blocked.limited and len(llm.calls) == 2
    assert (await agent.answer(2, "Bo", "hello")).limited is False  # other people are unaffected


async def test_global_budget_switches_to_knowledge_only_mode(make_agent, db, settings):
    llm = ScriptedLLM([say("ok")])
    agent, _ = await make_agent(llm)
    settings.global_daily_budget_usd = 0.01
    db.record_usage(99, 1000, 1000, 0.02)
    out = await agent.answer(1, "Ana", "how long is the warranty?")
    assert llm.calls == [] and out.limited
    assert "2 year warranty" in out.text and "budget" in out.text


async def test_runaway_tool_loop_is_capped(make_agent):
    loop = Reply(tool_calls=[ToolCall("c", "find_products", {"query": "buds"})])
    llm = ScriptedLLM([loop] * 10)
    agent, _ = await make_agent(llm)
    out = await agent.answer(1, "Ana", "buds?")
    assert len(llm.calls) == 4
    assert "rephrase" in out.text


async def test_handoff_event_reaches_the_caller(make_agent):
    llm = ScriptedLLM([Reply(tool_calls=[ToolCall("c", "talk_to_human", {"summary": "Left earbud is dead"})]), say("A person will join you.")])
    agent, _ = await make_agent(llm)
    out = await agent.answer(5, "Mia", "my earbud is dead, I want a person")
    assert out.events == [{"type": "handoff", "id": 1}]


async def test_without_a_model_the_bot_answers_from_the_knowledge_base(make_agent):
    agent, _ = await make_agent()
    out = await agent.answer(1, "Ana", "how much is express shipping?")
    assert "9.90 EUR" in out.text
    nothing = await agent.answer(1, "Ana", "tell me a joke about penguins")
    assert "/human" in nothing.text


# ----------------------------------------------------------------------- web
def test_admin_requires_a_password_and_webhook_requires_the_secret(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/admin").status_code == 401
        assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
        page = client.get("/admin", auth=("admin", "pw"))
        assert page.status_code == 200 and "Spend today" in page.text

        assert client.post("/telegram/webhook", json={}).status_code == 403
        right = {"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret}
        assert client.post("/telegram/webhook", json={}, headers=right).status_code == 503  # no bot token in tests


async def test_model_failure_falls_back_to_the_help_articles(make_agent):
    class Broken(ScriptedLLM):
        async def chat(self, messages, tools, max_tokens):
            raise RuntimeError("429 no credits")

    agent, _ = await make_agent(Broken([]))
    out = await agent.answer(1, "Ana", "how long is the warranty?")
    assert "2 year warranty" in out.text and "unavailable" in out.text
