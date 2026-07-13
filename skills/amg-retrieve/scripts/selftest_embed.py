#!/usr/bin/env python3
"""
selftest_embed.py - proves embedding seed enrichment works and is safe.

Uses a STUB embedder (deterministic, no model download) so it runs anywhere. The stub
maps any text mentioning a retry/failure concept -- in English OR Russian -- to the
same vector, simulating the semantic match that lexical BM25 cannot make across a
paraphrase / translation.

Checks:
  1. fallback : with no embedder, retrieval is pure BM25 and MISSES a gold node whose
                summary shares no words with the query (recall 0) -- the motivation.
  2. uplift   : with the stub embedder, that same gold node is seeded and recovered
                (recall 1). The PPR spread is unchanged; only the seed improved.
  3. cache    : node embeddings are cached and recomputed only when a node's text
                changes (hash-gated), not on every query.
  4. gate     : embeddings.enabled = off is respected even when a backend is loadable.
  5. multiling: a non-English working_language defaults to the multilingual model.
  6. off-vs-on: eval_retrieval.run threads the cfg; cross-language recall lifts on->1.

Run:  python selftest_embed.py
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import embed
import retrieve as R

try:
    import yaml
except ImportError:
    sys.stderr.write("needs PyYAML\n"); raise

QUERY = "how are payment failures handled with retries"
GOLD = "code:src/pay.py::retry_handler"

NODES = [
    # gold: Russian summary, no lexical overlap with the English query -> BM25 ~ 0
    (GOLD, "function", "Логика повторных попыток при сбое платёжного шлюза"),
    # lexical distractors: share 'payment' with the query -> BM25 > 0
    ("code:src/pay.py::charge", "function", "Handle the payment charge request"),
    ("code:src/pay.py::refund", "function", "Process a payment refund"),
    # unrelated
    ("code:src/auth.py::login", "function", "Authenticate a user login"),
]


class StubEmbedder:
    """Maps retry/failure-concept text (EN or RU) to one vector, everything else to
    an orthogonal one. Counts encode() calls to verify caching."""
    name = "stub"
    CONCEPT = re.compile(r"retr|повтор|fail|сбо")

    def __init__(self):
        self.calls = 0
        self.encoded = 0

    def encode(self, texts):
        self.calls += 1
        self.encoded += len(texts)
        out = []
        for t in texts:
            out.append([1.0, 0.0, 0.0] if self.CONCEPT.search(t.lower()) else [0.0, 1.0, 0.0])
        return out


def build_store() -> Path:
    root = Path(tempfile.mkdtemp(prefix="amg-emb-"))
    (root / "nodes" / "code").mkdir(parents=True)
    (root / "config.yml").write_text(
        "active: true\nretrieval:\n  embeddings:\n    enabled: auto\n    blend: 0.5\n",
        encoding="utf-8")
    for nid, typ, summary in NODES:
        meta = {"id": nid, "type": typ, "summary": summary, "status": "active",
                "edges": [], "part_of": []}
        slug = nid.split(":", 1)[-1].replace("/", "_").replace("::", "__")
        (root / "nodes" / "code" / f"{slug}.md").write_text(
            "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
            + "---\nbody\n", encoding="utf-8")
    return root


def recall_at_gold(root: Path) -> float:
    cfg = R.load_config(root)
    res = R.retrieve(root, QUERY, config=cfg, write_pack=False, log_coactivation=False)
    k = 1
    top = [nid for nid, _ in res["ranked"][:k]]
    return 1.0 if GOLD in top else 0.0


def test_multilingual_default() -> None:
    """get_embedder picks the multilingual default for a non-English working_language,
    the English default for English, and an explicit model always wins."""
    picked = {}

    class RecModel:
        name = "rec"
        def __init__(self, model): picked["model"] = model
        def encode(self, texts): return [[1.0, 0.0] for _ in texts]

    saved = embed._BACKENDS.copy()
    embed._BACKENDS["model2vec"] = RecModel
    try:
        embed.get_embedder({"embeddings": {"enabled": "on", "backend": "model2vec"},
                            "working_language": "en"})
        assert picked["model"] == embed._DEFAULT_MODEL["model2vec"], picked
        embed.get_embedder({"embeddings": {"enabled": "on", "backend": "model2vec"},
                            "working_language": "ru"})
        assert picked["model"] == embed._DEFAULT_MODEL_MULTILINGUAL["model2vec"], picked
        embed.get_embedder({"embeddings": {"enabled": "on", "backend": "model2vec",
                                           "model": "custom/x"}, "working_language": "ru"})
        assert picked["model"] == "custom/x", picked
    finally:
        embed._BACKENDS.clear(); embed._BACKENDS.update(saved)
    print("PASS  multilingual default: ru->multilingual, en->english, explicit model wins")


def test_eval_compare_offon() -> None:
    """eval_retrieval.run threads the cfg's embeddings setting: an EN query over a RU
    summary is missed off and recovered on — the off-vs-on harness."""
    import eval_retrieval as EV
    root = Path(tempfile.mkdtemp(prefix="amg-xlang-"))
    saved = embed._BACKENDS.copy()
    try:
        (root / "nodes" / "code").mkdir(parents=True)
        (root / "config.yml").write_text("active: true\nworking_language: ru\n", encoding="utf-8")
        for nid, summ in (("code:src/g.py::gw", "Списание средств во внешний платёжный шлюз"),
                          ("code:src/u.py::u", "External helper utility for logging output")):
            meta = {"id": nid, "type": "function", "summary": summ, "status": "active",
                    "edges": [], "part_of": []}
            slug = nid.split(":", 1)[-1].replace("/", "_").replace("::", "__")
            (root / "nodes" / "code" / f"{slug}.md").write_text(
                "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n",
                encoding="utf-8")
        cases = [{"id": "x", "query": "charge the external payment gateway",
                  "gold_ids": ["code:src/g.py::gw"]}]

        class GatewayStub:                 # bridges EN<->RU on the gateway/charge concept
            name = "gw"
            CONCEPT = re.compile(r"gateway|шлюз|charge|списан|payment|плат")
            def __init__(self, model=None): pass
            def encode(self, texts):
                return [[1.0, 0.0] if self.CONCEPT.search(t.lower()) else [0.0, 1.0]
                        for t in texts]
        embed._BACKENDS["model2vec"] = GatewayStub

        base = R.load_config(root)
        off = {**base, "embeddings": {**(base.get("embeddings") or {}), "enabled": "off"}}
        on = {**base, "embeddings": {**(base.get("embeddings") or {}), "enabled": "on",
                                     "backend": "model2vec", "blend": 0.9}}
        r_off = EV.run(root, cases, off)["aggregate"]["amg"]["recall"]
        r_on = EV.run(root, cases, on)["aggregate"]["amg"]["recall"]
        assert r_off == 0.0, f"EN query must miss the RU gold without embeddings, got {r_off}"
        assert r_on == 1.0, f"multilingual seed must recover the RU gold, got {r_on}"
        print(f"PASS  eval off-vs-on: cross-language recall {r_off:.0f} -> {r_on:.0f}")
    finally:
        embed._BACKENDS.clear(); embed._BACKENDS.update(saved)
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    root = build_store()
    orig = embed.get_embedder
    try:
        # 1. fallback: no embedder -> pure BM25 -> paraphrased gold missed
        embed.get_embedder = lambda cfg: None
        r_off = recall_at_gold(root)
        assert r_off == 0.0, f"expected BM25 to miss the paraphrase, got recall {r_off}"
        print("PASS  fallback: no model -> pure BM25, paraphrased gold missed (recall 0)")

        # 2. uplift: stub semantic embedder -> gold recovered
        stub = StubEmbedder()
        embed.get_embedder = lambda cfg: stub
        r_on = recall_at_gold(root)
        assert r_on == 1.0, f"expected embeddings to recover the gold node, got {r_on}"
        print(f"PASS  uplift: semantic seed recovers gold (recall {r_off:.0f} -> {r_on:.0f})")

        # 3. cache: re-embeds only changed nodes
        cache = root / "cache" / "embeddings.json"
        cache.unlink(missing_ok=True)
        nodes = R.load_nodes(root)
        s2 = StubEmbedder()
        embed.node_embeddings(s2, nodes, cache)            # cold: embeds all
        first = s2.encoded
        embed.node_embeddings(s2, nodes, cache)            # warm: embeds none
        assert s2.encoded == first, "warm cache should re-embed nothing"
        # change one node's text -> exactly one re-embed
        nodes[GOLD]["summary"] = nodes[GOLD]["summary"] + " (edited)"
        embed.node_embeddings(s2, nodes, cache)
        assert s2.encoded == first + 1, "only the changed node should be re-embedded"
        print(f"PASS  cache: cold embeds {first}, warm re-embeds 0, one edit re-embeds 1")

        # 4. gate: enabled=off returns None even with a loadable backend
        embed.get_embedder = orig
        saved = embed._BACKENDS.copy()
        embed._BACKENDS["model2vec"] = lambda model: StubEmbedder()   # pretend installed
        try:
            assert embed.get_embedder({"embeddings": {"enabled": "off"}}) is None
            assert embed.get_embedder({"embeddings": {"enabled": False}}) is None   # YAML `off`
            assert embed.get_embedder(
                {"embeddings": {"enabled": "auto", "backend": "model2vec"}}) is not None
            assert embed.get_embedder(
                {"embeddings": {"enabled": True, "backend": "model2vec"}}) is not None  # YAML `on`
        finally:
            embed._BACKENDS.clear(); embed._BACKENDS.update(saved)
        print("PASS  gate: enabled=off disables embeddings; auto uses an installed backend")

        test_multilingual_default()
        test_eval_compare_offon()

        print("\nALL EMBEDDING-SEEDING CHECKS PASSED")
    finally:
        embed.get_embedder = orig
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
