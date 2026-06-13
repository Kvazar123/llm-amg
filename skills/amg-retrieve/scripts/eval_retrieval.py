#!/usr/bin/env python3
"""
eval_retrieval.py — measure AMG retrieval quality and compare it to a lexical
(RAG-like) baseline. This is the harness that turns "is it better than RAG?" from
an opinion into a number, and gives you a signal to tune weights / thresholds /
the salience rubric against later.

Metrics per case (gold = the node ids that *should* be in the pack):
  recall      = |retrieved ∩ gold| / |gold|        did we surface what's needed?
  precision   = |retrieved ∩ gold| / |retrieved|   how much of the pack was on-target?
  hop-recall  = recall restricted to gold nodes that DON'T lexically match the
                query (reachable only via edges). This isolates exactly what
                spreading activation adds over plain lexical/vector top-k.

Two retrievers are compared at matched exposure K (= size of the AMG pack):
  * lexical : top-K by BM25 only  (a stand-in for vanilla RAG top-k)
  * amg     : query-biased Personalized PageRank pack (retrieve.py)

Usage:
  python eval_retrieval.py --make-demo /tmp/amg-demo     # build labeled graph + run
  python eval_retrieval.py --store <.../.claude/amg> --cases cases.json
  python eval_retrieval.py --store <...> --cases cases.json --out results.json
  python eval_retrieval.py --compare-embeddings /tmp/amg-xlang   # off vs on (xlang demo)
  python eval_retrieval.py --compare-embeddings --store <...> --cases cases.json

cases.json format:
  [ {"id": "...", "query": "...", "gold_ids": ["code:...", "doc:..."], "note": "..."} ]
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

import yaml

import retrieve as R

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def lexical_topk(rel: Dict[str, float], k: int) -> List[str]:
    return [nid for nid, _ in sorted(rel.items(), key=lambda kv: kv[1], reverse=True)][:k]


def evaluate_case(store_root: Path, case: dict, cfg: dict) -> dict:
    gold = set(case["gold_ids"])
    res = R.retrieve(store_root, case["query"], config=cfg,
                     write_pack=False, log_coactivation=False)

    # R-precision style: retrieve as many as there are gold nodes, and compare
    # AMG (ranked by activation) head-to-head with lexical (ranked by BM25).
    K = len(gold)
    amg_rank = [nid for nid, _ in res["ranked"]]
    amg_set = set(amg_rank[:K])

    rel = res["relevance"]
    lex_rank = lexical_topk(rel, len(rel))
    lex_set = set(lex_rank[:K])

    # Gold nodes that are NOT lexically reachable within K -> the multi-hop subset.
    rank_of = {nid: i for i, nid in enumerate(lex_rank)}
    hop_gold = {g for g in gold if rank_of.get(g, 10**9) >= K}

    # Secondary: does the assembled pack itself contain the gold?
    tiers = res["tiers"]
    pack_set = set(tiers.get("strategic", []) + tiers.get("tactical", []) +
                   tiers.get("operational", []) + tiers.get("periphery", []))

    def recall(s):    return len(s & gold) / len(gold) if gold else 0.0
    def prec(s):      return len(s & gold) / len(s) if s else 0.0
    def hoprec(s):    return len(s & hop_gold) / len(hop_gold) if hop_gold else None

    return {
        "id": case.get("id", case["query"][:30]),
        "query": case["query"],
        "K": K,
        "gold": len(gold),
        "hop_gold": len(hop_gold),
        "amg":     {"recall": recall(amg_set), "precision": prec(amg_set),
                    "hop_recall": hoprec(amg_set)},
        "lexical": {"recall": recall(lex_set), "precision": prec(lex_set),
                    "hop_recall": hoprec(lex_set)},
        "pack_recall": recall(pack_set),
        "missed_by_amg": sorted(gold - amg_set),
    }


def run(store_root: Path, cases: List[dict], cfg: dict = None) -> dict:
    cfg = cfg or R.load_config(store_root)
    rows = [evaluate_case(store_root, c, cfg) for c in cases]

    def mean(getter, where=lambda r: True):
        vals = [getter(r) for r in rows if where(r) and getter(r) is not None]
        return sum(vals) / len(vals) if vals else None

    agg = {
        "cases": len(rows),
        "amg":     {"recall": mean(lambda r: r["amg"]["recall"]),
                    "precision": mean(lambda r: r["amg"]["precision"]),
                    "hop_recall": mean(lambda r: r["amg"]["hop_recall"])},
        "lexical": {"recall": mean(lambda r: r["lexical"]["recall"]),
                    "precision": mean(lambda r: r["lexical"]["precision"]),
                    "hop_recall": mean(lambda r: r["lexical"]["hop_recall"])},
        "amg_pack_recall": mean(lambda r: r["pack_recall"]),
    }
    return {"per_case": rows, "aggregate": agg}


def print_report(report: dict) -> None:
    print(f"{'case':<22}{'K':>3}{'gold':>5}{'hop':>4}   "
          f"{'AMG rec':>8}{'lex rec':>8}   {'AMG prec':>9}{'lex prec':>9}   "
          f"{'AMG hop':>8}{'lex hop':>8}")
    print("-" * 100)
    for r in report["per_case"]:
        a, l = r["amg"], r["lexical"]
        hop_a = "n/a" if a["hop_recall"] is None else f"{a['hop_recall']:.2f}"
        hop_l = "n/a" if l["hop_recall"] is None else f"{l['hop_recall']:.2f}"
        print(f"{r['id']:<22}{r['K']:>3}{r['gold']:>5}{r['hop_gold']:>4}   "
              f"{a['recall']:>8.2f}{l['recall']:>8.2f}   "
              f"{a['precision']:>9.2f}{l['precision']:>9.2f}   "
              f"{hop_a:>8}{hop_l:>8}")
    g = report["aggregate"]
    print("-" * 100)

    def fmt(x): return "n/a" if x is None else f"{x:.2f}"
    print(f"{'MEAN':<22}{'':>3}{'':>5}{'':>4}   "
          f"{fmt(g['amg']['recall']):>8}{fmt(g['lexical']['recall']):>8}   "
          f"{fmt(g['amg']['precision']):>9}{fmt(g['lexical']['precision']):>9}   "
          f"{fmt(g['amg']['hop_recall']):>8}{fmt(g['lexical']['hop_recall']):>8}")
    print("\nhop-recall = recall on gold nodes that DON'T lexically match the query")
    print("(reachable only via edges). It isolates what spreading activation adds.")
    print(f"AMG pack recall (gold present in the assembled pack): {fmt(g['amg_pack_recall'])}")


# --------------------------------------------------------------------------- #
# Synthetic labeled graph for an immediate, reproducible demonstration
# --------------------------------------------------------------------------- #

def _node(nid, typ, summary, body="", edges=None, part_of=None,
          source_path=None, lineno=None, status="active") -> str:
    meta = {"id": nid, "type": typ, "summary": summary,
            "edges": edges or [], "part_of": part_of or [], "status": status}
    if source_path:
        meta["source_path"] = source_path
        meta["lineno"] = lineno or 1
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body}".rstrip() + "\n"


def build_demo_store(root: Path, xlang: bool = False) -> List[dict]:
    """A two-subsystem graph designed so two billing gold nodes are reachable
    ONLY via edges (no lexical overlap with the query) — a clean multi-hop test.
    The auth subsystem is a distractor that must NOT be retrieved (precision). A
    superseded older policy exercises the status prior, and (when xlang=True) an
    isolated Russian-summary node reachable from an English query only by meaning
    exercises the embeddings off-vs-on comparison."""
    nd = root / "nodes"
    for sub in ("code", "doc", "notes", "_hubs"):
        (nd / sub).mkdir(parents=True, exist_ok=True)
    # embeddings off by default so --make-demo is deterministic and offline; the
    # --compare-embeddings mode flips this on via a cfg override at run time.
    (root / "config.yml").write_text(
        "active: true\nworking_language: ru\nretrieval:\n  embeddings:\n    enabled: off\n")

    def w(rel_dir, fname, text):
        (nd / rel_dir / fname).write_text(text, encoding="utf-8")

    # --- billing subsystem ---
    w("_hubs", "billing.md", _node(
        "hub:billing", "hub", "Billing subsystem: invoicing and card payments.",
        edges=[{"rel": "documents", "to": "code:src/billing.py", "w": 0.6}]))
    w("code", "billing_mod.md", _node(
        "code:src/billing.py", "module", "Billing module: invoicing and card payments.",
        source_path="src/billing.py", lineno=1,
        edges=[{"rel": "defines", "to": "code:src/billing.py::charge_card", "w": 1.0},
               {"rel": "defines", "to": "code:src/billing.py::compute_total", "w": 1.0}],
        part_of=[{"topic": "hub:billing", "w": 1.0}]))
    w("code", "charge_card.md", _node(
        "code:src/billing.py::charge_card", "function",
        "Charges the customer's card for the invoice amount.",
        source_path="src/billing.py", lineno=20,
        edges=[{"rel": "calls", "to": "code:src/billing.py::compute_total", "w": 0.9},
               {"rel": "relates_to", "to": "notes:decisions/retry-policy", "w": 0.85}],
        part_of=[{"topic": "hub:billing", "w": 1.0}]))
    # multi-hop gold #1: no query words ("declined/card/payment") here.
    w("code", "compute_total.md", _node(
        "code:src/billing.py::compute_total", "function",
        "Aggregates line items into the amount owed.",
        source_path="src/billing.py", lineno=40,
        part_of=[{"topic": "hub:billing", "w": 1.0}]))
    w("doc", "billing_doc.md", _node(
        "doc:doc/billing.md::overview", "section",
        "How billing charges customers and handles declined cards.",
        body="Charging flow and what happens when a card is declined.",
        edges=[{"rel": "documents", "to": "code:src/billing.py", "w": 0.9},
               {"rel": "relates_to", "to": "notes:decisions/retry-policy", "w": 0.7}]))
    # multi-hop gold #2: a decision, no query words; reached via relates_to.
    w("notes", "retry_policy.md", _node(
        "notes:decisions/retry-policy", "decision",
        "Retry policy: three attempts with exponential backoff before surfacing an error.",
        body="Decision: wrap the gateway call in a 3x retry with exponential backoff.",
        edges=[{"rel": "supersedes", "to": "notes:decisions/retry-policy-v1", "w": 0.9}]))
    # superseded near-duplicate, deliberately the STRONGER lexical match for the
    # 'failed charge' query: a naive lexical ranker picks it; only the status prior
    # (x0.2) keeps the active decision above it. This is what makes the case sharp.
    w("notes", "retry_policy_v1.md", _node(
        "notes:decisions/retry-policy-v1", "decision",
        "Retry policy for a failed charge: re-attempt the failed charge twice with "
        "backoff, then surface the error.",
        body="Superseded: two retries, no backoff.", status="superseded"))

    # --- lexical distractors: match the query's words but are NOT connected to the
    # charge flow and are NOT gold. A pure lexical/RAG ranker is fooled by these and
    # spends its budget on them, pushing the true multi-hop nodes out of top-K.
    w("doc", "refunds_doc.md", _node(
        "doc:doc/refunds.md::overview", "section",
        "Refunding a customer's card payment after a declined charge dispute."))
    w("code", "card_validator.md", _node(
        "code:src/payments/validate.py::check_card", "function",
        "Validates a customer's card number and payment details before checkout.",
        source_path="src/payments/validate.py", lineno=5))
    w("doc", "payments_faq.md", _node(
        "doc:doc/faq.md::payments", "section",
        "Customer FAQ about declined cards, card payments, and what to do."))

    # --- auth subsystem (distractor, must stay out of the pack) ---
    w("_hubs", "auth.md", _node("hub:auth", "hub", "Authentication and sessions."))
    w("code", "auth_mod.md", _node(
        "code:src/auth.py", "module", "Auth module: login and session handling.",
        source_path="src/auth.py", lineno=1,
        edges=[{"rel": "defines", "to": "code:src/auth.py::login", "w": 1.0}],
        part_of=[{"topic": "hub:auth", "w": 1.0}]))
    w("code", "login.md", _node(
        "code:src/auth.py::login", "function", "Authenticates a user via password.",
        source_path="src/auth.py", lineno=10, part_of=[{"topic": "hub:auth", "w": 1.0}]))
    w("doc", "auth_doc.md", _node(
        "doc:doc/auth.md::overview", "section", "User authentication and session lifetime.",
        edges=[{"rel": "documents", "to": "code:src/auth.py", "w": 0.9}]))

    cases = [{
        "id": "declined-card-charge",
        "query": "what happens when a customer's card payment is declined",
        "gold_ids": [
            "hub:billing",
            "code:src/billing.py",
            "code:src/billing.py::charge_card",
            "doc:doc/billing.md::overview",
            "notes:decisions/retry-policy",        # multi-hop (via relates_to)
            "code:src/billing.py::compute_total",  # multi-hop (via calls)
        ],
        "note": "2 gold nodes share no words with the query (reached only via edges); "
                "3 disconnected distractors share words but are not gold.",
    }, {
        "id": "retry-policy-current",
        "query": "retry policy for a failed charge with backoff",
        "gold_ids": ["notes:decisions/retry-policy"],
        "note": "retry-policy-v1 is a SUPERSEDED near-duplicate and the STRONGER lexical "
                "match; lexical top-1 picks it, only the status prior keeps the active "
                "decision on top (AMG recall 1 vs lexical 0).",
    }]
    if xlang:
        # English query, Russian summary, zero lexical overlap, and isolated (no edges)
        # so it activates ONLY via the seed: missed with BM25, found with a multilingual
        # embedding. Reached only when embeddings are on -> the off-vs-on comparison.
        w("code", "gateway_ru.md", _node(
            "code:src/billing.py::call_gateway", "function",
            "Отправляет запрос на списание средств во внешний платёжный шлюз и разбирает ответ."))
        cases.append({
            "id": "xlang-gateway",
            "query": "send the charge request to the external payment gateway",
            "gold_ids": ["code:src/billing.py::call_gateway"],
            "note": "EN query over a RU summary, no shared words — recovered only with a "
                    "multilingual embedding seed (run --compare-embeddings).",
        })
    (root / "cases.json").write_text(json.dumps(cases, indent=2))
    return cases


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    args = argv[1:]
    if "--make-demo" in args:
        root = Path(args[args.index("--make-demo") + 1]).resolve()
        root.mkdir(parents=True, exist_ok=True)
        cases = build_demo_store(root)
        print(f"built demo graph at {root}  ({len(cases)} cases)\n")
        report = run(root, cases)
        print_report(report)
        return 0

    if "--compare-embeddings" in args:
        # Size the embedding uplift: run the cases with embeddings OFF then ON.
        if "--store" in args and "--cases" in args:
            store = Path(args[args.index("--store") + 1]).resolve()
            cases = json.loads(Path(args[args.index("--cases") + 1]).read_text(encoding="utf-8"))
        else:
            j = args.index("--compare-embeddings")
            path = args[j + 1] if j + 1 < len(args) and not args[j + 1].startswith("-") else None
            store = Path(path).resolve() if path else Path(tempfile.mkdtemp(prefix="amg-xlang-"))
            store.mkdir(parents=True, exist_ok=True)
            cases = build_demo_store(store, xlang=True)
            print(f"built xlang demo graph at {store}\n")
        base = R.load_config(store)
        for label, enabled in (("OFF", "off"), ("ON", "on")):
            cfg = {**base, "embeddings": {**(base.get("embeddings") or {}), "enabled": enabled}}
            print(f"=== embeddings {label} ===")
            print_report(run(store, cases, cfg))
            print()
        return 0

    if "--store" not in args or "--cases" not in args:
        print(__doc__)
        return 2
    store = Path(args[args.index("--store") + 1]).resolve()
    cases = json.loads(Path(args[args.index("--cases") + 1]).read_text(encoding="utf-8"))
    report = run(store, cases)
    print_report(report)
    if "--out" in args:
        Path(args[args.index("--out") + 1]).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args[args.index('--out') + 1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
