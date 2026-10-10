"""Search over sfhs.com, the Student-Parent Handbook and the Course Scheduling Guide (prebuilt by
scripts/build_index.py), plus live sources added while running (Daily Announcements)."""
import json
import logging
import threading

import numpy as np

from . import config

log = logging.getLogger("sfmate.rag")

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_embedder = None
_embedder_lock = threading.Lock()
docs, doc_embs = [], np.zeros((0, 768), dtype=np.float32)   # the prebuilt index
EXTRA_SOURCES = {}   # name -> (chunks, embeddings), replaced in one assignment so searches never see half an index


def chunk_text(text, max_chars=1200, overlap=200):
    """Pack paragraphs into ~max_chars chunks, carrying the tail of each chunk into the next."""
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) + 1 > max_chars:
            chunks.append(cur)
            cur = cur[-overlap:]
        cur = f"{cur}\n{p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def set_embedder(embedder):
    """Tests use this to swap in a stand-in; normally the real model is loaded on first use."""
    global _embedder
    _embedder = embedder


def embedder():
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            from sentence_transformers import SentenceTransformer   # heavy import: only when actually needed
            _embedder = SentenceTransformer(config.EMBED_MODEL_ID, device="cpu")
        return _embedder


def embed(texts, query=False):
    if query:
        texts = [BGE_QUERY_PREFIX + t for t in texts]
    return np.asarray(embedder().encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)


def load_index(data_dir=config.DATA_DIR):
    global docs, doc_embs
    chunks_path, embs_path = data_dir / "chunks.json", data_dir / "embeddings.npy"
    if not (chunks_path.exists() and embs_path.exists()):
        log.warning("no search index in %s: run `python -m scripts.build_index`. Answers will only use announcements.",
                    data_dir)
        return
    meta = json.loads((data_dir / "meta.json").read_text()) if (data_dir / "meta.json").exists() else {}
    if meta.get("embed_model", config.EMBED_MODEL_ID) != config.EMBED_MODEL_ID:
        raise RuntimeError(f"index was built with {meta['embed_model']}, but the app uses {config.EMBED_MODEL_ID}")
    docs = json.loads(chunks_path.read_text())
    doc_embs = np.load(embs_path).astype(np.float32)
    log.info("search index: %d chunks (built %s)", len(docs), meta.get("built_at", "?"))


def retrieve(query, k=5):
    q = embed([query], query=True)[0]
    pool, scores = list(docs), [doc_embs @ q] if len(docs) else []
    for extra_chunks, extra_embs in list(EXTRA_SOURCES.values()):
        if extra_chunks:
            pool += extra_chunks
            scores.append(extra_embs @ q)
    if not pool:
        return []
    scores = np.concatenate(scores)
    return [(pool[i], float(scores[i])) for i in np.argsort(-scores)[:k]]


def retrieve_where(query, k, keep):
    """Like retrieve, but only over prebuilt chunks where keep(chunk) is true (one isolated source)."""
    idx = [i for i, c in enumerate(docs) if keep(c)]
    if not idx:
        return []
    scores = doc_embs[idx] @ embed([query], query=True)[0]
    return [(docs[idx[i]], float(scores[i])) for i in np.argsort(-scores)[:k]]
