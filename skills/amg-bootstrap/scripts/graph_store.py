#!/usr/bin/env python3
"""
graph_store.py — crash-safe, idempotent storage layer for the AMG graph.

This is the foundation that guarantees consistency under failure. It knows
nothing about nodes, edges, or LLMs; it is a generic transactional store over
text files living under the AMG root (`.claude/amg/`). Higher layers
(reconcile.py, the subagents) express *what* should change; this layer
guarantees that a change is applied all-or-nothing and can always be brought
to a consistent state after a crash.

Design (see ../references/consistency-model.md for the full rationale):

  1. Atomic single-file writes
     A file is written to a temp file in the same directory, fsync'd, then
     atomically renamed over the target (os.replace). A reader therefore never
     sees a half-written file: it sees either the old bytes or the new bytes.

  2. Write-ahead journal with DECLARATIVE redo (not undo)
     A logical change usually touches several files (a node plus the neighbors
     whose inbound references it rewrites). We stage the *desired end state* of
     every touched file as a content
     blob, record the intent durably, then apply. Because the journal records
     the target content (addressed by hash), re-applying it any number of times
     converges to the same state. Recovery is therefore "redo to target",
     which needs no undo log and is naturally idempotent.

  3. Recovery
     On startup we scan the journal. A transaction whose intent never became
     durable is discarded (nothing was applied). A transaction whose intent is
     durable but which was not marked committed is re-applied (idempotent) and
     then committed. A committed-but-not-cleaned transaction is just cleaned up.
     Every interruption point converges to a consistent state.

  4. Single-writer lock
     Writes take an exclusive lock (O_EXCL lockfile with stale detection).
     Reads are lock-free and safe, because every write is atomic per file.

CLI:
    python graph_store.py init       # create the store directory skeleton
    python graph_store.py recover     # replay any unfinished transaction
    python graph_store.py verify [--repair]
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import uuid
import hashlib
import tempfile
import contextlib
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# --------------------------------------------------------------------------- #
# Low-level atomic primitives
# --------------------------------------------------------------------------- #

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def _fsync_dir(dir_path: Path) -> None:
    """Flush a directory entry so a rename/create survives a crash."""
    # Directory fsync is a no-op / unsupported on some platforms (e.g. Windows);
    # treat failure as best-effort.
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (temp + fsync + rename + dir fsync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic on POSIX and within a Windows volume
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_delete(path: Path) -> None:
    """Remove a file if present (idempotent)."""
    try:
        os.remove(path)
        _fsync_dir(path.parent)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Store-root resolution (roadmap 4.9: agent dir is a parameter, not `.claude`)
# --------------------------------------------------------------------------- #

def resolve_amg_root(cli_root: os.PathLike[str] | str | None = None,
                     start: os.PathLike[str] | str | None = None) -> Path:
    """Resolve the AMG store root (<agent_dir>/amg). Chain, first hit wins:

      1. explicit --root (the agent dir, e.g. `.claude` or `.agents`);
      2. the AMG_AGENT_DIR environment variable (same meaning);
      3. the first ancestor of `start` (default: cwd) holding amg/config.yml —
         that directory IS the agent dir. The known agent-dir presets are also
         probed one level down (<ancestor>/{.claude,.agents}/amg/config.yml): this
         is what lets a globally-installed engine find a project's graph from the
         project's cwd, under any environment (a fully custom dir uses AMG_AGENT_DIR);
      4. the engine's own location (scripts live at
         <agent_dir>/skills/amg-bootstrap/scripts -> <agent_dir>/amg), only if
         that amg/ already exists — covers the dev layout and a local install
         without letting a global engine hijack a fresh project's default;
      5. the default: <start>/.claude/amg.
    """
    if cli_root:
        return Path(cli_root).resolve() / "amg"
    env = os.environ.get("AMG_AGENT_DIR")
    if env:
        return Path(env).resolve() / "amg"
    base = Path(start).resolve() if start else Path.cwd()
    for d in (base, *base.parents):
        if (d / "amg" / "config.yml").exists():
            return d / "amg"
        for adir in (".claude", ".agents"):      # known agent-dir presets (1.32)
            if (d / adir / "amg" / "config.yml").exists():
                return d / adir / "amg"
    engine_root = Path(__file__).resolve().parents[3] / "amg"
    if engine_root.is_dir():
        return engine_root
    return base / ".claude" / "amg"


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

class StoreLockError(RuntimeError):
    pass


class GraphStore:
    """Transactional, recoverable store rooted at `.claude/amg/`."""

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root).resolve()
        self.journal_dir = self.root / "journal"
        self.nodes_dir = self.root / "nodes"
        self.lock_path = self.root / "LOCK"

    # -- setup --------------------------------------------------------------- #

    def init(self) -> None:
        for d in (self.root, self.journal_dir, self.nodes_dir,
                  self.nodes_dir / "code", self.nodes_dir / "doc",
                  self.nodes_dir / "data", self.nodes_dir / "notes",
                  self.nodes_dir / "_hubs"):
            d.mkdir(parents=True, exist_ok=True)

    def abspath(self, relpath: str) -> Path:
        p = (self.root / relpath).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"path escapes store root: {relpath}")
        return p

    # -- locking ------------------------------------------------------------- #

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)          # POSIX liveness probe
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # exists, owned by someone else
        except (AttributeError, OSError):
            return True              # unknown (e.g. Windows): assume alive

    @contextlib.contextmanager
    def lock(self, stale_seconds: int = 3600,
             wait_seconds: float = 0.0) -> Iterator["GraphStore"]:
        """Acquire the single-writer lock. Reclaims a STALE lock — a dead pid on THIS
        host, or any lock older than stale_seconds. A live lock held by ANOTHER host (a
        shared folder, stage 16) is reclaimed only by age, never by a local pid probe."""
        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + wait_seconds
        acquired = False
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                               "ts": time.time()}, f)
                acquired = True
                break
            except FileExistsError:
                if self._lock_is_stale(stale_seconds):
                    atomic_delete(self.lock_path)
                    continue
                if time.time() >= deadline:
                    raise StoreLockError(
                        f"AMG store is locked by another writer ({self._lock_owner()}). "
                        f"If that process is dead, delete {self.lock_path}.")
                time.sleep(0.2)
        try:
            yield self
        finally:
            if acquired:
                atomic_delete(self.lock_path)

    def _lock_owner(self) -> str:
        try:
            info = json.loads(self.lock_path.read_text(encoding="utf-8"))
            return f"pid={info.get('pid')} host={info.get('host')}"
        except Exception:
            return "unknown"

    def _lock_is_stale(self, stale_seconds: int) -> bool:
        try:
            info = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except Exception:
            # Unreadable/garbage lock: treat as stale.
            return True
        same_host = info.get("host") == socket.gethostname()
        # Our own lock, held on purpose (e.g. verify --repair runs under it): never
        # stale. Without this, verify --repair flags the lock it just took.
        if same_host and info.get("pid") == os.getpid():
            return False
        # A pid liveness probe is only meaningful on the SAME host: pid 1234 on another
        # machine is unrelated to pid 1234 here. On a SHARED FOLDER across hosts, probing
        # a foreign pid locally would FALSELY steal a teammate's live lock (it almost
        # never exists in this machine's process table). So only the same host may reclaim
        # a dead-pid lock; a foreign host's lock is reclaimed solely by the age threshold
        # (a genuinely abandoned lock still frees itself after stale_seconds).
        if same_host and not self._pid_alive(int(info.get("pid", -1))):
            return True
        age = time.time() - float(info.get("ts", 0))
        return age > stale_seconds

    # -- transactions -------------------------------------------------------- #

    def transaction(self) -> "Transaction":
        return Transaction(self)

    # -- recovery ------------------------------------------------------------ #

    def recover(self) -> List[str]:
        """Bring the store to a consistent state. Returns ids of handled txns.

        Safe to run at any time, repeatedly. This is the function that makes a
        crash a non-event.
        """
        handled: List[str] = []
        if not self.journal_dir.exists():
            return handled
        for txdir in sorted(self.journal_dir.iterdir()):
            if not txdir.is_dir():
                continue
            manifest_path = txdir / "manifest.json"
            committed = (txdir / "COMMITTED").exists()
            if not manifest_path.exists():
                # Intent never became durable -> nothing was applied. Discard.
                shutil.rmtree(txdir, ignore_errors=True)
                handled.append(txdir.name + ":discarded")
                continue
            if committed:
                # Already applied; just finish cleanup.
                shutil.rmtree(txdir, ignore_errors=True)
                handled.append(txdir.name + ":cleaned")
                continue
            # Durable intent, not committed -> redo to target (idempotent).
            manifest = json.loads(manifest_path.read_text())
            self._apply_manifest(manifest, txdir)
            atomic_write_text(txdir / "COMMITTED", str(time.time()))
            shutil.rmtree(txdir, ignore_errors=True)
            handled.append(txdir.name + ":redone")
        return handled

    def _apply_manifest(self, manifest: Dict[str, Any], txdir: Path,
                        _fault_after_apply_ops: Optional[int] = None) -> None:
        """Apply a transaction's declarative end state. Idempotent."""
        applied = 0
        for op in manifest["ops"]:
            target = self.abspath(op["path"])
            if op["op"] == "write":
                want_sha = op["sha"]
                # Idempotency guard: if the target already holds the desired
                # content, applying again is a no-op.
                if target.exists() and sha256_bytes(target.read_bytes()) == want_sha:
                    pass
                else:
                    blob = txdir / "blobs" / want_sha
                    atomic_write_bytes(target, blob.read_bytes())
            elif op["op"] == "delete":
                atomic_delete(target)
            else:
                raise ValueError(f"unknown op {op['op']!r}")
            applied += 1
            if _fault_after_apply_ops is not None and applied >= _fault_after_apply_ops:
                # TEST HOOK ONLY: simulate a crash partway through apply.
                raise _SimulatedCrash(f"fault injected after {applied} ops")

    # -- verification -------------------------------------------------------- #

    def verify(self, repair: bool = False, referenced_paths: Optional[List[str]] = None
               ) -> Dict[str, List[str]]:
        """Check store invariants. With repair=True, fix what is safely fixable."""
        problems: Dict[str, List[str]] = {"pending_transactions": [], "stale_lock": [],
                                          "dangling_references": []}

        pending = [d.name for d in self.journal_dir.iterdir()
                   if self.journal_dir.exists() and d.is_dir()] if self.journal_dir.exists() else []
        if pending:
            problems["pending_transactions"] = pending
            if repair:
                self.recover()
                problems["pending_transactions_resolved"] = pending

        if self.lock_path.exists() and self._lock_is_stale(stale_seconds=0 if repair else 3600):
            problems["stale_lock"] = [self._lock_owner()]
            if repair:
                atomic_delete(self.lock_path)

        if referenced_paths:
            missing = [p for p in referenced_paths if not self.abspath(p).exists()]
            problems["dangling_references"] = missing

        return problems

    # -- action log ---------------------------------------------------------- #

    def append_log(self, source: str, msg: str, txid: Optional[str] = None,
                   archive_dir: str = "archive", max_lines: int = 500,
                   keep_tail: int = 100) -> None:
        """Append one human-readable audit line to log.md, transactionally.

        Line shape: `## [<ts>] <txid|-> <source> | <msg>`. Unlike a plain append:

          * the write goes through a transaction (atomic, crash-safe, idempotent
            on replay — the whole log is staged as one content-addressed blob);
          * if a line carrying this `txid` is already present it is a no-op
            (de-dup by txid), so a re-run of the same committed change never
            double-logs;
          * the log is BOUNDED: once it would exceed `max_lines`, the older lines
            are rotated into `<archive_dir>/log-<ts>.md` and only the tail stays
            live, so a long-lived graph never rewrites an unbounded file per write.

        Call it under the single-writer lock the caller already holds (it opens its
        own transaction; commit() does not take the lock). Best-effort: any failure
        is swallowed — graph integrity rests on the journal, not on log.md, so a
        missing or torn audit line is harmless.
        """
        try:
            log_rel = "log.md"
            log_path = self.root / log_rel
            lines = (log_path.read_text(encoding="utf-8").splitlines()
                     if log_path.exists() else [])
            if txid and any(txid in ln for ln in lines):
                return                                  # already logged this txn
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"## [{ts}] {txid or '-'} {source} | {msg}")
            tx = self.transaction()
            if len(lines) > max_lines:                  # rotate, keep the tail live
                cut = len(lines) - keep_tail
                arch = f"{archive_dir}/log-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.md"
                tx.write(arch, "\n".join(lines[:cut]) + "\n")
                lines = lines[cut:]
            tx.write(log_rel, "\n".join(lines) + "\n")
            tx.commit()
        except Exception:                               # best-effort: never break the caller
            pass


class _SimulatedCrash(RuntimeError):
    """Raised only by the test hook to mimic a process kill mid-apply."""


class Transaction:
    """Collects a set of file writes/deletes and applies them atomically."""

    def __init__(self, store: GraphStore):
        self.store = store
        self._writes: Dict[str, bytes] = {}
        self._deletes: List[str] = []

    def write(self, relpath: str, content: str | bytes) -> "Transaction":
        data = content.encode("utf-8") if isinstance(content, str) else content
        self._writes[relpath] = data
        self._deletes = [d for d in self._deletes if d != relpath]
        return self

    def delete(self, relpath: str) -> "Transaction":
        self._deletes.append(relpath)
        self._writes.pop(relpath, None)
        return self

    def is_empty(self) -> bool:
        return not self._writes and not self._deletes

    def node_paths(self) -> tuple[List[str], List[str]]:
        """The (written, deleted) relpaths under nodes/ this transaction carries — the
        hook the disposable read-index (index_store) uses to refresh just what changed.
        Filtering to nodes/ keeps the index in step with the graph without reacting to
        journal/work/log writes. This stays a plain path filter — the store knows
        nothing about the index itself (that lives in the retrieve layer)."""
        written = [p for p in self._writes if p.startswith("nodes/")]
        deleted = [p for p in self._deletes if p.startswith("nodes/")]
        return written, deleted

    def commit(self, _fault_after_apply_ops: Optional[int] = None) -> Optional[str]:
        """Make the staged changes durable, atomic, and crash-recoverable.

        Returns the transaction id, or None if nothing to do.
        """
        if self.is_empty():
            return None

        txid = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        txdir = self.store.journal_dir / txid
        blobs = txdir / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)

        ops: List[Dict[str, Any]] = []
        # 1. Stage every write as a content-addressed blob.
        for relpath, data in self._writes.items():
            sha = sha256_bytes(data)
            blob = blobs / sha
            if not blob.exists():
                atomic_write_bytes(blob, data)
            ops.append({"op": "write", "path": relpath, "sha": sha})
        for relpath in self._deletes:
            ops.append({"op": "delete", "path": relpath})

        # 2. Record the intent durably. Until this rename lands, NOTHING has
        #    been applied to the live store, so a crash here is a clean no-op.
        manifest = {"txid": txid, "ts": time.time(), "ops": ops}
        atomic_write_text(txdir / "manifest.json", json.dumps(manifest, indent=2))

        # 3. Apply the declarative end state (idempotent).
        self.store._apply_manifest(manifest, txdir,
                                   _fault_after_apply_ops=_fault_after_apply_ops)

        # 4. Mark committed, then clean up the journal entry.
        atomic_write_text(txdir / "COMMITTED", str(time.time()))
        shutil.rmtree(txdir, ignore_errors=True)
        return txid


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: List[str]) -> int:
    args = list(argv[1:])
    cli_root: Optional[str] = None
    if "--root" in args:                       # explicit agent dir
        i = args.index("--root")
        cli_root = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "help"
    # Resolve the store like reconcile/consolidate/notes: --root -> AMG_AGENT_DIR ->
    # config search upward from cwd -> engine location -> default .claude. This is what
    # lets a GLOBAL engine (~/.claude/skills) heal a project's LOCAL graph from the
    # project's cwd. AMG_ROOT stays as an explicit full-path override (e.g. for tests).
    env_root = os.environ.get("AMG_ROOT")
    root = Path(env_root) if env_root else resolve_amg_root(cli_root, Path.cwd())
    store = GraphStore(root)

    if cmd == "init":
        store.init()
        print(f"initialized AMG store at {store.root}")
        return 0

    if cmd == "recover":
        try:
            with store.lock():
                handled = store.recover()
        except StoreLockError as exc:        # shared folder: a live writer holds it
            print(json.dumps({"error": "locked", "detail": str(exc)}, indent=2))
            return 1
        print(json.dumps({"recovered": handled}, indent=2))
        return 0

    if cmd == "verify":
        repair = "--repair" in argv
        try:
            ctx = store.lock() if repair else contextlib.nullcontext(store)
            with ctx:
                problems = store.verify(repair=repair)
        except StoreLockError as exc:        # --repair under a live foreign lock: skip cleanly
            print(json.dumps({"error": "locked", "detail": str(exc)}, indent=2))
            return 1
        clean = all(not v for k, v in problems.items() if not k.endswith("_resolved"))
        print(json.dumps({"clean": clean, "problems": problems}, indent=2))
        return 0 if clean else 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
