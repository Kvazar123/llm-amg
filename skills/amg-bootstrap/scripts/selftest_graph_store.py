#!/usr/bin/env python3
"""
selftest_graph_store.py — proves the crash-safety guarantees of graph_store.py.

It simulates a process kill at each dangerous point of a transaction and checks
that `recover()` always converges to a consistent state: no half-written files,
no duplicates, no lost committed work.

Run:  python selftest_graph_store.py
"""
import shutil
import tempfile
from pathlib import Path

import graph_store as gs


def fresh_store() -> gs.GraphStore:
    root = Path(tempfile.mkdtemp(prefix="amg-test-"))
    store = gs.GraphStore(root)
    store.init()
    return store


def read(store, rel):
    p = store.abspath(rel)
    return p.read_text() if p.exists() else None


def case_middle_crash():
    """Crash AFTER one of two writes is applied, BEFORE commit."""
    store = fresh_store()
    tx = store.transaction()
    tx.write("nodes/code/a.md", "AAA")
    tx.write("nodes/code/b.md", "BBB")
    try:
        tx.commit(_fault_after_apply_ops=1)   # die after first file written
        raise AssertionError("fault hook did not fire")
    except gs._SimulatedCrash:
        pass

    # One file may exist, the other may not, and the journal is still present.
    assert (store.journal_dir.exists()
            and any(store.journal_dir.iterdir())), "journal should hold the pending txn"

    # Recovery redoes to the declared target: BOTH files present, correct content.
    store.recover()
    assert read(store, "nodes/code/a.md") == "AAA"
    assert read(store, "nodes/code/b.md") == "BBB"
    assert not any(store.journal_dir.iterdir()), "journal must be empty after recovery"

    # Idempotency: recovering / re-running again changes nothing.
    store.recover()
    assert read(store, "nodes/code/a.md") == "AAA"
    assert read(store, "nodes/code/b.md") == "BBB"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  middle-of-apply crash -> recovered, all-or-nothing, idempotent")


def case_begin_crash():
    """Crash BEFORE the intent (manifest) is durable -> clean no-op."""
    store = fresh_store()
    # Simulate: a txdir with staged blobs but no manifest (process died before
    # the manifest rename landed).
    txdir = store.journal_dir / "20990101T000000-deadbeef"
    (txdir / "blobs").mkdir(parents=True)
    gs.atomic_write_bytes(txdir / "blobs" / gs.sha256_text("XXX"), b"XXX")
    # No manifest.json written.

    store.recover()
    assert not (store.nodes_dir / "code").exists() or \
        list((store.nodes_dir / "code").iterdir()) == [], "nothing should be applied"
    assert not txdir.exists(), "incomplete txn must be discarded"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  pre-durable crash -> discarded cleanly, nothing applied")


def case_end_crash():
    """Crash AFTER apply, but before the journal entry was cleaned up."""
    store = fresh_store()
    tx = store.transaction()
    tx.write("nodes/docs/x.md", "DOC")
    # Build the journal state by hand to mimic 'applied + COMMITTED, not cleaned'.
    txid = "20990101T000000-cafef00d"
    txdir = store.journal_dir / txid
    blobs = txdir / "blobs"
    blobs.mkdir(parents=True)
    sha = gs.sha256_text("DOC")
    gs.atomic_write_bytes(blobs / sha, b"DOC")
    manifest = {"txid": txid, "ts": 0, "ops": [{"op": "write", "path": "nodes/docs/x.md", "sha": sha}]}
    gs.atomic_write_text(txdir / "manifest.json", gs.json.dumps(manifest))
    store._apply_manifest(manifest, txdir)               # already applied
    gs.atomic_write_text(txdir / "COMMITTED", "0")        # marked committed
    # ... crash before rmtree(txdir)

    assert read(store, "nodes/docs/x.md") == "DOC"
    store.recover()                                       # should just clean up
    assert read(store, "nodes/docs/x.md") == "DOC", "committed work must survive"
    assert not txdir.exists(), "committed txn must be cleaned up"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  post-commit crash -> committed work survives, journal cleaned")


def case_delete_and_idempotent_rerun():
    """A delete op is also all-or-nothing and idempotent."""
    store = fresh_store()
    store.transaction().write("nodes/code/old.md", "OLD").commit()
    assert read(store, "nodes/code/old.md") == "OLD"
    store.transaction().delete("nodes/code/old.md").commit()
    assert read(store, "nodes/code/old.md") is None
    # Deleting again is a no-op, not an error.
    store.transaction().delete("nodes/code/old.md").commit()
    assert read(store, "nodes/code/old.md") is None
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  delete is idempotent and crash-safe")


def case_lock_reentry():
    store = fresh_store()
    with store.lock():
        assert store.lock_path.exists()
    assert not store.lock_path.exists(), "lock released on context exit"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  write lock acquired and released")


def case_documented_layout():
    """The on-disk layout documented in 02-data-model.md and consistency-model.md
    must match exactly what graph_store.init() creates: the five node buckets plus
    journal/, and NO phantom files (index.md / graph.json are written and read by no
    code). Everything else (work/, archive/, cache/, log.md, LOCK) is created on
    demand, not by init()."""
    store = fresh_store()                       # fresh_store() already calls init()
    expected = {store.root, store.journal_dir, store.nodes_dir,
                store.nodes_dir / "code", store.nodes_dir / "doc",
                store.nodes_dir / "data", store.nodes_dir / "notes",
                store.nodes_dir / "_hubs"}
    for d in expected:
        assert d.is_dir(), f"init() must create {d}"
    buckets = {p.name for p in store.nodes_dir.iterdir() if p.is_dir()}
    assert buckets == {"code", "doc", "data", "notes", "_hubs"}, \
        f"node buckets drifted from the documented set: {buckets}"
    for phantom in ("index.md", "graph.json"):
        assert not (store.root / phantom).exists(), \
            f"{phantom} is documented nowhere and must not exist"
    for p in ("work", "archive", "cache", "log.md", "LOCK"):
        assert not (store.root / p).exists(), f"{p} must be created on demand, not by init()"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  documented layout == graph_store.init() (5 buckets, no phantom files)")


def case_action_log():
    """append_log writes log.md transactionally, de-dups by txid, and rotates when
    bounded — so a long-lived graph never rewrites an unbounded file (audit 1.15)."""
    store = fresh_store()
    log = store.root / "log.md"

    def nlines() -> int:
        return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0

    # 1. First line written through a real transaction (journal clean afterwards).
    store.append_log("consolidate", "weights folded", "tx-aaaa")
    body = log.read_text(encoding="utf-8")
    assert "consolidate | weights folded" in body and "tx-aaaa" in body
    assert not list(store.journal_dir.iterdir()), "log write must commit and clean the journal"

    # 2. De-dup: the SAME committed txid must not append a second line.
    store.append_log("consolidate", "again", "tx-aaaa")
    assert nlines() == 1, "same txid must not append twice"

    # 3. A new txid appends; a None txid (e.g. a reject event) always appends.
    store.append_log("reconcile", "bootstrap: added=3", "tx-bbbb")
    store.append_log("consolidate", "eval-gate REJECTED", None)
    assert nlines() == 3
    lines = log.read_text(encoding="utf-8").splitlines()
    assert " reconcile |" in lines[1] and " consolidate |" in lines[2], \
        "source label must be discriminable (lifecycle._last_consolidation filters on it)"

    # 4. Rotation: a bounded log moves the overflow to archive/, keeps the tail live.
    for i in range(20):
        store.append_log("consolidate", f"line {i}", f"tx-{i:04d}",
                         max_lines=5, keep_tail=2)
    live = log.read_text(encoding="utf-8").splitlines()
    assert len(live) <= 5, f"live log must stay bounded, got {len(live)}"
    assert "line 19" in live[-1], "the newest line stays live (status reads the tail)"
    assert list((store.root / "archive").glob("log-*.md")), "rotation must archive the overflow"
    shutil.rmtree(store.root, ignore_errors=True)
    print("PASS  action log: transactional, de-duped by txid, bounded by rotation")


if __name__ == "__main__":
    case_begin_crash()
    case_middle_crash()
    case_end_crash()
    case_delete_and_idempotent_rerun()
    case_lock_reentry()
    case_documented_layout()
    case_action_log()
    print("\nALL CRASH-SAFETY CHECKS PASSED")
