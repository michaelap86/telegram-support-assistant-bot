import pytest

from app.agent import Agent
from app.config import Settings
from app.db import Db
from app.kb import KnowledgeBase
from app.llm import NoLLM, Reply
from app.seed import seed_demo


class ScriptedLLM:
    """Plays back prepared replies and remembers what it was asked."""

    available = True

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def chat(self, messages, tools, max_tokens):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        return self.replies.pop(0)

    async def embed(self, texts):
        raise RuntimeError("embeddings are not scripted")


def say(text, prompt=100, completion=20):
    return Reply(content=text, prompt_tokens=prompt, completion_tokens=completion)


@pytest.fixture
def settings():
    return Settings(_env_file=None, db_path=":memory:", knowledge_dir="knowledge", admin_password="pw", telegram_token="")


@pytest.fixture
def db():
    d = Db(":memory:")
    seed_demo(d)
    return d


@pytest.fixture
def make_agent(settings, db):
    async def factory(llm=None):
        llm = llm or NoLLM()
        kb = KnowledgeBase(db, llm, settings.knowledge_dir)
        await kb.sync()
        return Agent(settings, db, kb, llm), kb

    return factory
