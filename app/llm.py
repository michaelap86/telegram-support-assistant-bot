"""Thin wrapper around the language model so the rest of the app never touches the SDK directly."""
import json
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLM(Protocol):
    available: bool

    async def chat(self, messages: list[dict], tools: list[dict], max_tokens: int) -> Reply: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class NoLLM:
    """Used when no API key is set: the bot then answers straight from the knowledge base."""

    available = False

    async def chat(self, messages, tools, max_tokens):
        raise RuntimeError("No language model configured")

    async def embed(self, texts):
        raise RuntimeError("No language model configured")


class OpenAILLM:
    available = True

    def __init__(self, api_key: str, model: str, embed_model: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, timeout=30, max_retries=2)
        self.model, self.embed_model = model, embed_model

    async def chat(self, messages, tools, max_tokens):
        kwargs = {"model": self.model, "messages": messages, "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
        res = await self.client.chat.completions.create(**kwargs)
        msg = res.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(tc.id, tc.function.name, args))
        usage = res.usage
        return Reply(
            content=msg.content or "",
            tool_calls=calls,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )

    async def embed(self, texts):
        res = await self.client.embeddings.create(model=self.embed_model, input=texts)
        return [d.embedding for d in res.data]


def build_llm(settings) -> LLM:
    if settings.openai_api_key:
        return OpenAILLM(settings.openai_api_key, settings.chat_model, settings.embed_model)
    return NoLLM()
