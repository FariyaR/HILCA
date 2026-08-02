"""Hybrid retrieval over the run's ContextMaterial (master file v2, batch 1/3).

The master file asks for a "Hybrid Retrieval Pipeline" (semantic + keyword) with
"Targeted Chunk Retrieval per Turn" — never feeding the entire document into
every agent turn. Both halves live here, behind one search() interface:

  - the KEYWORD half: BM25 (Okapi) plus an exact-phrase boost;
  - the VECTOR half: embeddings over the same paragraph chunks, cosine-scored
    and fused with BM25 by a weighted sum (HILCA_HYBRID_ALPHA).

The vector store is in-memory and dependency-free: chunk vectors are embedded
once at index build (OpenAI embeddings when configured, an offline hashed
bag-of-tokens embedder otherwise) and scored with pure-Python cosine — at
HILCA's corpus scale (one reference doc plus a handful of scraped sources)
that is the whole store. Retrieval NEVER fails a run: any embedding failure
degrades that index (or that single query) to BM25-only, logged.

It powers:
  - targeted per-turn context in stateless mode (rounds 2+ get the top-k chunks
    relevant to the CCU's current directives instead of the full re-injection);
  - the Gap-Analysis loop-back (each gap the CCU identifies becomes a query;
    the retrieved chunks are fed into the final wrap-up).

Env:
  HILCA_EMBEDDINGS    auto (default) | openai | hash | off
                      auto -> OpenAI embeddings when OPENAI_API_KEY is set and
                      LLM_PROVIDER is not mock; the offline hash embedder
                      otherwise. off -> keyword half only.
  HILCA_EMBED_MODEL   OpenAI embedding model (default text-embedding-3-small)
  HILCA_HYBRID_ALPHA  weight of the vector half in the fused score, 0..1
                      (default 0.5; 0 = pure BM25, 1 = pure vector)
"""
from __future__ import annotations

import math
import os
import re
import zlib
from typing import Callable, List, Optional

# BM25 constants (standard Okapi defaults).
_K1 = 1.5
_B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Chunking: paragraph-aligned, bounded size so a retrieved chunk is always a
# readable, self-contained span (the master file's AutoContext note: a chunk
# should carry enough surrounding context to be understood on its own).
CHUNK_CHARS = 1200
CHUNK_MIN = 200


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def chunk_text(text: str) -> List[str]:
    """Split into paragraph-aligned chunks of roughly CHUNK_CHARS characters."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > CHUNK_CHARS:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
        # a single paragraph larger than the cap is split hard
        while len(buf) > 2 * CHUNK_CHARS:
            chunks.append(buf[:CHUNK_CHARS])
            buf = buf[CHUNK_CHARS:]
    if buf:
        # merge a tiny tail into the previous chunk rather than emitting a scrap
        if chunks and len(buf) < CHUNK_MIN:
            chunks[-1] += "\n\n" + buf
        else:
            chunks.append(buf)
    return chunks


# --------------------------------------------------------------- embedders --
def _unit(v: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v


class HashEmbedder:
    """Deterministic offline embeddings: hashed bag-of-tokens (unigrams and
    bigrams) bucketed into DIMS dimensions with a crc32 sign trick, then
    L2-normalized. Not semantic — it approximates lexical overlap — but it
    keeps the vector half fully exercisable with no key and no network
    (mock/demo/tests), and is the safe fallback when a real embedding
    backend is not configured."""

    DIMS = 256

    def name(self) -> str:
        return "hash"

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.DIMS
            toks = _tokens(text)
            for gram in toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]:
                h = zlib.crc32(gram.encode("utf-8"))
                v[h % self.DIMS] += 1.0 if (h >> 16) & 1 else -1.0
            out.append(_unit(v))
        return out


class OpenAIEmbedder:
    """The real vector half: OpenAI embeddings (HILCA_EMBED_MODEL), batched,
    unit-normalized. Lazy import so mock/offline installs need no SDK."""

    BATCH = 128
    MAX_CHARS = 8000  # stay safely under the embedding model's token cap

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.getenv("HILCA_EMBED_MODEL", "text-embedding-3-small")

    def name(self) -> str:
        return f"openai:{self.model}"

    def embed(self, texts: List[str]) -> List[List[float]]:
        from openai import OpenAI  # lazy

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        out: List[List[float]] = []
        for i in range(0, len(texts), self.BATCH):
            batch = [t[: self.MAX_CHARS] for t in texts[i: i + self.BATCH]]
            resp = client.embeddings.create(model=self.model, input=batch)
            out.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        return [_unit(v) for v in out]


def make_embedder():
    """Resolve the vector-half backend from HILCA_EMBEDDINGS (see module doc)."""
    mode = os.getenv("HILCA_EMBEDDINGS", "auto").strip().lower()
    if mode in ("off", "none", "0"):
        return None
    if mode == "openai":
        return OpenAIEmbedder()
    if mode in ("hash", "offline"):
        return HashEmbedder()
    # auto: real embeddings only when a key exists and we're not in mock mode
    # (tests and the offline demo must never touch the network).
    if os.getenv("OPENAI_API_KEY") and os.getenv("LLM_PROVIDER", "mock").lower() != "mock":
        return OpenAIEmbedder()
    return HashEmbedder()


def _hybrid_alpha() -> float:
    try:
        return min(1.0, max(0.0, float(os.getenv("HILCA_HYBRID_ALPHA", "0.5"))))
    except ValueError:
        return 0.5


def _max_norm(scores: List[float]) -> List[float]:
    top = max(scores) if scores else 0.0
    return [s / top for s in scores] if top > 0 else [0.0] * len(scores)


class HybridIndex:
    """Hybrid retrieval over chunked text: BM25 + exact-phrase (keyword half)
    fused with embedding cosine (vector half) by a weighted sum."""

    def __init__(self, text: str, embedder="auto",
                 log: Optional[Callable[[str], None]] = None):
        self.chunks = chunk_text(text)
        self._docs = [_tokens(c) for c in self.chunks]
        self._doc_lens = [len(d) for d in self._docs]
        self._avg_len = (sum(self._doc_lens) / len(self._docs)) if self._docs else 0.0
        # document frequency per term
        self._df: dict[str, int] = {}
        for doc in self._docs:
            for term in set(doc):
                self._df[term] = self._df.get(term, 0) + 1
        self._tf: List[dict[str, int]] = []
        for doc in self._docs:
            tf: dict[str, int] = {}
            for term in doc:
                tf[term] = tf.get(term, 0) + 1
            self._tf.append(tf)
        # Vector half: embed the corpus once at build. Failure is never fatal —
        # the index degrades to BM25-only and says so.
        self._log = log or (lambda m: None)
        self._embedder = make_embedder() if embedder == "auto" else embedder
        self._vecs: Optional[List[List[float]]] = None
        self._query_cache: dict[str, List[float]] = {}
        if self._embedder is not None and self.chunks:
            try:
                self._vecs = self._embedder.embed(self.chunks)
            except Exception as exc:
                self._log(f"Vector half unavailable ({exc}); retrieval continues BM25-only")
                self._embedder = None

    @property
    def vector_backend(self) -> Optional[str]:
        """Backend name of the active vector half, or None when BM25-only."""
        return self._embedder.name() if self._embedder and self._vecs else None

    def _bm25(self, query_terms: List[str], i: int) -> float:
        score = 0.0
        n = len(self._docs)
        dl = self._doc_lens[i] or 1
        for term in query_terms:
            df = self._df.get(term)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            tf = self._tf[i].get(term, 0)
            score += idf * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * dl / self._avg_len))
        return score

    def _cosine_scores(self, query: str) -> Optional[List[float]]:
        """Cosine of the query against every chunk vector (unit vectors, so a
        dot product suffices). None disables the vector half for this search."""
        if self._embedder is None or self._vecs is None:
            return None
        qv = self._query_cache.get(query)
        if qv is None:
            try:
                qv = self._embedder.embed([query])[0]
            except Exception as exc:
                self._log(f"Query embedding failed ({exc}); this search runs BM25-only")
                return None
            if len(self._query_cache) > 256:
                self._query_cache.clear()
            self._query_cache[query] = qv
        # negatives clamped: anti-correlated chunks must not drag fused scores
        return [max(0.0, sum(a * b for a, b in zip(qv, dv))) for dv in self._vecs]

    def search(self, query: str, k: int = 5) -> List[str]:
        """Top-k chunks for the query: BM25 + phrase boost, fused with the
        vector half when it is available."""
        if not self.chunks:
            return []
        q_terms = _tokens(query)
        phrase = query.strip().lower()
        keyword = []
        for i, chunk in enumerate(self.chunks):
            s = self._bm25(q_terms, i)
            if len(phrase) > 12 and phrase in chunk.lower():
                s *= 1.5  # exact matches outrank bag-of-words
            keyword.append(s)
        cosine = self._cosine_scores(query)
        if cosine is None:
            fused = keyword
        else:
            alpha = _hybrid_alpha()
            kn, cn = _max_norm(keyword), _max_norm(cosine)
            fused = [(1 - alpha) * kn[i] + alpha * cn[i] for i in range(len(self.chunks))]
        scored = sorted(((s, i) for i, s in enumerate(fused)), key=lambda t: (-t[0], t[1]))
        return [self.chunks[i] for s, i in scored[:k] if s > 0] or [self.chunks[0]]

    def search_joined(self, query: str, k: int = 5, header: str = "") -> str:
        parts = self.search(query, k)
        body = "\n\n---\n\n".join(parts)
        return f"{header}\n{body}" if header else body
