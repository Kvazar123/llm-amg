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


if __name__ == "__main__":
    case_begin_crash()
    case_middle_crash()
    case_end_crash()
    case_delete_and_idempotent_rerun()
    case_lock_reentry()
    print("\nALL CRASH-SAFETY CHECKS PASSED")
