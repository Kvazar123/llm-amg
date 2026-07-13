#!/usr/bin/env python3
"""
embed.py - OPTIONAL semantic seed enrichment for AMG retrieval.

Lexical seeding (BM25) misses paraphrases: ask "how are payment failures handled"
when the code says "retry on gateway error" and the word overlap is near zero, so the
right node never gets seeded. Embeddings add SEMANTIC similarity to the seed, so the
teleport vector lights up by meaning as well as by words.

This module ONLY produces a per-node similarity to the query. retrieve.py blends it
into the teleport vector; the PPR spread and the pack assembly are untouched. That
isolation is deliberate: the embedding effect is measurable on its own (recall with
vs without), and a bad embedding model can never corrupt multi-hop structure.

OPTIONAL with graceful fallback. If no embedding backend is installed, get_embedder()
returns None and retrieval stays pure-BM25 (unchanged behaviour). Backends, lightest
first:
    model2vec              - static embeddings, fast, CPU-only, no torch
                             pip install model2vec
    sentence-transformers  - full transformer embeddings (heavier; needs torch)
                             pip install sentence-transformers

Node embeddings are cached on disk and recomputed only when a node's embedded text
changes (the same content-hash gating used everywhere else in AMG), so the cost is
paid once per changed node, not per query.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

# Keep Hugging Face hub quiet: its progress bars / telemetry otherwise pollute the
# eval and retrieval output on every model load.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Default models per backend (downloaded once on first use; offline -> fallback).
# AMG's embedding seed is OPTIONAL, light enrichment over BM25 (markdown stays the
# source of truth), so defaults favor FAST, CPU-friendly, retrieval/multilingual-tuned
# models — deliberately NOT the heavyweight ~8B MTEB leaders (Qwen3-Embedding,
# Llama-Embed-Nemotron, Gemini), which contradict the light-by-design seed. Pick by
# measuring per graph (`eval_retrieval.py --compare-embeddings`); any HF id can be set
# via retrieval.embeddings.{backend,model}.
_DEFAULT_MODEL = {
    # model2vec = static, no torch. potion-retrieval-32M is MinishLab's best static
    # RETRIEVAL model — exactly this seed's job (beats the older potion-base-*).
    "model2vec": "minishlab/potion-retrieval-32M",
    "sentence-transformers": "sentence-transformers/all-MiniLM-L6-v2",
}
# For a non-English working_language, default to a MULTILINGUAL model per backend so
# cross-language seeding (an English query over Russian summaries) works without the
# user naming a model. If the model can't load (offline / not cached), get_embedder
# falls through to the next backend and finally to None (pure BM25) — no hard failure.
_DEFAULT_MODEL_MULTILINGUAL = {
    # potion-multilingual-128M: best static multilingual model, 101 languages (MinishLab).
    "model2vec": "minishlab/potion-multilingual-128M",
    # paraphrase-multilingual-MiniLM-L12-v2: robust, loads with a plain SentenceTransformer
    # call. Higher quality but heavier alternative (set via config): Alibaba-NLP/gte-multilingual-base.
    "sentence-transformers": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
}


def _unit(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


def dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

class _Model2Vec:
    name = "model2vec"

    def __init__(self, model: str):
        from model2vec import StaticModel
        # Skip a Hub re-download when the model is cached (force_download defaults to
        # True in model2vec); an older version without the kwarg loads the plain way.
        try:
            self.m = StaticModel.from_pretrained(model, force_download=False)
        except TypeError:
            self.m = StaticModel.from_pretrained(model)

    def encode(self, texts: List[str]) -> List[List[float]]:
        vecs = self.m.encode(texts)
        return [_unit([float(x) for x in row]) for row in vecs]


class _SentenceTransformers:
    name = "sentence-transformers"

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer
        # Offline-first: every retrieve.py call is a fresh process, and a cached model
        # must not cost a Hub round-trip per process (measured: the Hub check dwarfed
        # the model load itself). local_files_only skips the network entirely; when
        # the model is not cached yet — or the installed version predates the
        # parameter — fall back to the ordinary, downloading load.
        try:
            self.m = SentenceTransformer(model, local_files_only=True)
        except Exception:
            self.m = SentenceTransformer(model)

    def encode(self, texts: List[str]) -> List[List[float]]:
        vecs = self.m.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in row] for row in vecs]


# A loaded backend: any object exposing .encode(list[str]) -> list[list[float]].
_Embedder = Union[_Model2Vec, _SentenceTransformers]
_BACKENDS: Dict[str, Type[_Embedder]] = {
    "model2vec": _Model2Vec, "sentence-transformers": _SentenceTransformers}
_ORDER = ["model2vec", "sentence-transformers"]


def get_embedder(cfg: Dict[str, Any]) -> Optional[_Embedder]:
    """Return an embedder (object with .encode(list[str]) -> list[list[float]]),
    or None if embeddings are disabled or no backend is installed/loadable.
    `cfg` is the retrieval config; reads cfg['embeddings'] = {enabled, backend, model}
    and cfg['working_language'] — a non-English project defaults to a multilingual
    model per backend, so cross-language seeding works without naming a model."""
    emb = (cfg or {}).get("embeddings") or {}
    enabled = emb.get("enabled", "auto")
    # YAML parses bare `off`/`on` as booleans, not strings, so accept both forms.
    if enabled is False or str(enabled).strip().lower() in ("off", "false", "no", "0"):
        return None
    want = str(emb.get("backend", "auto")).lower()
    order = _ORDER if want in ("auto", "", None) else [want]
    model_override = emb.get("model") or ""
    lang = str((cfg or {}).get("working_language") or "en").strip().lower()
    defaults = _DEFAULT_MODEL if lang.startswith("en") else _DEFAULT_MODEL_MULTILINGUAL
    for backend in order:
        cls = _BACKENDS.get(backend)
        if cls is None:
            continue
        try:
            return cls(model_override or defaults[backend])
        except Exception:
            continue                       # not installed, or model unavailable offline
    return None


# --------------------------------------------------------------------------- #
# Node embedding cache (hash-gated)
# --------------------------------------------------------------------------- #

def node_text(node: Dict[str, Any]) -> str:
    """What we embed for a node: its summary plus its identifier (identifiers carry
    meaning), falling back to the id when there is no summary yet."""
    nid = str(node.get("id", ""))
    summary = str(node.get("summary") or "").strip()
    tail = nid.split(":", 1)[-1].replace("::", " ").replace("/", " ").replace("_", " ")
    return (summary + " " + tail).strip() or nid


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def node_embeddings(embedder: _Embedder, nodes: Dict[str, Dict[str, Any]],
                    cache_path: Path) -> Dict[str, List[float]]:
    """id -> unit vector. Reads/writes a JSON cache; only (re)embeds nodes whose
    embedded text changed since last time. Stable across runs."""
    cache: Dict[str, Dict[str, Any]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    texts = {nid: node_text(n) for nid, n in nodes.items()}
    hashes = {nid: _text_hash(t) for nid, t in texts.items()}

    stale = [nid for nid in nodes if cache.get(nid, {}).get("h") != hashes[nid]]
    if stale:
        fresh = embedder.encode([texts[nid] for nid in stale])
        for nid, vec in zip(stale, fresh):
            cache[nid] = {"h": hashes[nid], "v": [round(x, 6) for x in vec]}
        for nid in list(cache):           # drop entries for deleted nodes
            if nid not in nodes:
                cache.pop(nid, None)
        _save(cache_path, cache)

    return {nid: cache[nid]["v"] for nid in nodes if nid in cache}


def _save(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def seed_scores(embedder: Optional[_Embedder], nodes: Dict[str, Dict[str, Any]],
                query: str, cache_path: Path) -> Optional[Dict[str, float]]:
    """Per-node semantic similarity to the query in [0,1], or None if no embedder.
    Used by retrieve.py to blend into the teleport vector."""
    if embedder is None:
        return None
    vecs = node_embeddings(embedder, nodes, cache_path)
    if not vecs:
        return None
    qv = embedder.encode([query])[0]
    return {nid: max(0.0, dot(vecs[nid], qv)) for nid in vecs}


# --------------------------------------------------------------------------- #
# Diagnostic CLI:  python embed.py
# Reports which backends are installed, whether a model actually loads, and a
# cross-language sanity check (so you can see if the model is multilingual).
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    import importlib
    print("Embedding backends:")
    for b, mod in (("model2vec", "model2vec"),
                   ("sentence-transformers", "sentence_transformers")):
        try:
            importlib.import_module(mod)
            print(f"  {b:22} OK")
        except Exception as e:
            print(f"  {b:22} FAILED: {type(e).__name__}: {str(e)[:120]}")

    emb = get_embedder({"embeddings": {"enabled": "on", "backend": "auto"}})
    if emb is None:
        print("\nNo embedder loaded  ->  retrieval is pure BM25 (on == off).")
        print("If a backend shows 'installed' above, the MODEL failed to load — usually")
        print("no Hugging Face access on first download. Get the model files locally, or")
        print("set retrieval.embeddings.backend/model to one that is available offline.")
        return 1

    vecs = emb.encode(["routing", "роутинг", "banana fruit"])
    print(f"\nLoaded backend : {emb.name}")
    print(f"embedding dim  : {len(vecs[0])}")
    print(f"sim(routing, роутинг)     = {dot(vecs[0], vecs[1]):.3f}   (high => multilingual)")
    print(f"sim(routing, banana fruit)= {dot(vecs[0], vecs[2]):.3f}   (should be lower)")
    if dot(vecs[0], vecs[1]) < 0.4:
        print("\nNOTE: low cross-language similarity — this model is not multilingual.")
        print("For Russian content use e.g. backend: sentence-transformers,")
        print("model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
