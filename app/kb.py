"""Knowledge base: markdown files are split into sections and searched by meaning (embeddings)
or, when no language model is configured, by keywords."""
import hashlib
import json
import logging
import math
import re
from pathlib import Path

from .db import Db
from .llm import LLM

log = logging.getLogger("support-bot")

STOP = set(
    "a an and are as at be by can do does for from get how i if in is it me my of on or our so that the to "
    "was we what when where which who will with you your about tell please any there have has need want would like much".split()
)


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for w in words:
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        if len(w) > 5 and w.endswith("ing"):
            w = w[:-3]
        out.append(w)
    return out


def load_chunks(directory: str) -> list[dict]:
    chunks = []
    for path in sorted(Path(directory).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for n, part in enumerate(re.split(r"^## ", text, flags=re.M)[1:], start=1):
            title, _, body = part.partition("\n")
            body = body.strip()
            chunks.append(
                {
                    "id": f"{path.stem}#{n}",
                    "source": path.stem,
                    "title": title.strip(),
                    "body": body,
                    "hash": hashlib.sha1(f"{title}{body}".encode()).hexdigest(),
                }
            )
    return chunks


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class KnowledgeBase:
    def __init__(self, db: Db, llm: LLM, directory: str):
        self.db, self.llm, self.directory = db, llm, directory

    async def sync(self) -> int:
        """Loads the files, and (re)computes embeddings only for sections that changed."""
        chunks = load_chunks(self.directory)
        known = {r["id"]: r for r in self.db.all("SELECT id, hash, embedding FROM kb_chunks")}
        stale = []
        for c in chunks:
            old = known.get(c["id"])
            if not old or old["hash"] != c["hash"]:
                self.db.run(
                    "INSERT INTO kb_chunks (id, source, title, body, hash, embedding) VALUES (?,?,?,?,?,NULL) "
                    "ON CONFLICT(id) DO UPDATE SET source=excluded.source, title=excluded.title, "
                    "body=excluded.body, hash=excluded.hash, embedding=NULL",
                    (c["id"], c["source"], c["title"], c["body"], c["hash"]),
                )
                stale.append(c)
            elif not old["embedding"]:
                stale.append(c)
        keep = tuple(c["id"] for c in chunks) or ("",)
        self.db.run(f"DELETE FROM kb_chunks WHERE id NOT IN ({','.join('?' * len(keep))})", keep)

        if stale and self.llm.available:
            try:
                vectors = await self.llm.embed([f"{c['title']}\n{c['body']}" for c in stale])
            except Exception:  # the bot must still start, and search by keywords, if embeddings fail
                log.exception("could not compute embeddings")
            else:
                for c, vec in zip(stale, vectors):
                    self.db.run("UPDATE kb_chunks SET embedding=? WHERE id=?", (json.dumps(vec), c["id"]))
        return len(chunks)

    async def search(self, query: str, k: int = 3, strict: bool = True) -> list[dict]:
        rows = self.db.all("SELECT id, source, title, body, embedding FROM kb_chunks")
        if not rows:
            return []
        scored = None
        if self.llm.available and all(r["embedding"] for r in rows):
            try:
                (qv,) = await self.llm.embed([query])
                scored = [(cosine(qv, json.loads(r["embedding"])), r) for r in rows]
            except Exception:  # fall back to keywords if the embedding call fails
                scored = None
        semantic = scored is not None
        if scored is None:
            scored = [(self._keyword_score(query, r, rows), r) for r in rows]
        scored.sort(key=lambda s: s[0], reverse=True)
        # Cosine scores of unrelated text sit around 0.1-0.2, keyword scores need a real overlap
        floor = 0.28 if semantic else 0.5
        # strict=False keeps the best matches even below the floor: the model can judge relevance
        # itself, which matters for questions in other languages where similarity scores run lower.
        return [
            {"title": r["title"], "source": r["source"], "body": r["body"], "score": round(s, 3), "semantic": semantic, "relevant": s >= floor}
            for s, r in scored[:k]
            if s > 0 and (s >= floor or not strict)
        ]

    @staticmethod
    def _keyword_score(query: str, row: dict, rows: list[dict]) -> float:
        q = set(tokens(query))
        if not q:
            return 0.0
        body = set(tokens(row["body"]))
        title = set(tokens(row["title"]))
        total, matched = 0.0, 0
        for word in q:
            hit = word in title or word in body
            matched += hit
            docs = sum(1 for r in rows if word in tokens(r["title"] + " " + r["body"])) or 1
            idf = math.log(1 + len(rows) / docs)
            total += idf * ((2.0 if word in title else 0.0) + (1.0 if word in body else 0.0))
        # Most of the question has to appear in the section, otherwise it is just a coincidence
        if len(q) > 1 and matched / len(q) < 0.5:
            return 0.0
        return total / len(q)
