"""快照存储：核查结果整版入库，成功后原子替换现役版本，失败保留旧版。

- 每个快照自包含（源清单两版 + 依赖 + 报告），查询只读取 state='active' 的唯一版本，
  因此任何查询都不会混用两版数据。
- 新版本的解析/评估全部在写事务之外完成；替换仅一条事务内的
  UPDATE(archived) + INSERT(active)，失败回滚则旧版原样保留。
- WAL 模式保证核查（写入）进行时，旧版仍可被并发读取。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('active', 'archived', 'failed')),
    error      TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_state ON snapshots(state, id);
"""


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        conn = self._conn()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # -- 写 ---------------------------------------------------------------

    def publish(self, name: str, payload: dict[str, Any]) -> int:
        """整版替换：新快照置为 active，旧 active 置为 archived。事务失败则旧版保留。"""
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE snapshots SET state='archived' WHERE state='active'")
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, payload) VALUES (?,?, 'active', ?)",
                    (name, self._now(), blob))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def record_failed(self, name: str, error: str) -> int:
        """核查失败留痕，不影响现役版本。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO snapshots(name, created_at, state, error) VALUES (?,?, 'failed', ?)",
                    (name or "(未命名)", self._now(), error))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    # -- 读（只读现役版本，绝不混版） --------------------------------------

    def current(self) -> Optional[dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, name, created_at, payload FROM snapshots "
                "WHERE state='active' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            payload = json.loads(row["payload"])
            return {"id": row["id"], "name": row["name"],
                    "created_at": row["created_at"], "payload": payload}
        finally:
            conn.close()

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at, state, error FROM snapshots "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 观测数据契约与批次存储
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('active', 'archived', 'rejected')),
    error      TEXT,
    payload    TEXT
);
CREATE INDEX IF NOT EXISTS idx_contracts_state ON contracts(state, id);

CREATE TABLE IF NOT EXISTS batches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('published', 'quarantined')),
    contract_id      INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    contract_payload TEXT NOT NULL,
    total_rows       INTEGER NOT NULL,
    error_rows       INTEGER NOT NULL,
    header_errors    TEXT,
    quarantine       TEXT,
    rows_payload     TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_state ON batches(state, id);
"""


class ContractStore:
    """契约版本与观测批次存储。

    - 契约整版替换：新契约置 active，旧 active 置 archived，单事务完成；
      不兼容或格式错误的契约只留 rejected 痕迹，现役契约不受影响。
    - 每个批次（无论发布还是隔离）都固化校验时的契约快照
      （contract_id + version + 完整 payload），之后契约如何更新，
      查询该批次都只读到它自己绑定的快照，绝不混用。
    - 「上次可用批次」= 最新 state='published' 的批次；隔离批次只是留痕，
      不改变它。
    """

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        conn = self._conn()
        try:
            conn.executescript(CONTRACT_SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    # -- 契约写 -------------------------------------------------------------

    def publish_contract(self, version: str, payload: dict[str, Any]) -> int:
        """新契约置为现役，旧现役归档；事务失败则旧版保留。"""
        blob = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE contracts SET state='archived' WHERE state='active'")
                cur = conn.execute(
                    "INSERT INTO contracts(version, created_at, state, payload) "
                    "VALUES (?,?, 'active', ?)", (version, self._now(), blob))
                conn.commit()
                return int(cur.lastrowid)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def reject_contract(self, version: str, error: str) -> int:
        """不兼容/不合法契约留痕，不影响现役契约。"""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO contracts(version, created_at, state, error) "
                    "VALUES (?,?, 'rejected', ?)", (version, self._now(), error))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    # -- 契约读 -------------------------------------------------------------

    def active_contract(self) -> Optional[dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, version, created_at, payload FROM contracts "
                "WHERE state='active' ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            return {"id": row["id"], "version": row["version"],
                    "created_at": row["created_at"], "payload": json.loads(row["payload"])}
        finally:
            conn.close()

    def contract_history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, version, created_at, state, error FROM contracts "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- 批次写 -------------------------------------------------------------

    def _insert_batch(self, name: str, state: str, contract: dict[str, Any],
                      total_rows: int, error_rows: int, header_errors: list,
                      quarantine: Optional[list], rows: Optional[list]) -> int:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO batches(name, created_at, state, contract_id, "
                    "contract_version, contract_payload, total_rows, error_rows, "
                    "header_errors, quarantine, rows_payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name, self._now(), state, contract["id"], contract["version"],
                     json.dumps(contract["payload"], ensure_ascii=False),
                     total_rows, error_rows,
                     json.dumps(header_errors, ensure_ascii=False),
                     json.dumps(quarantine, ensure_ascii=False)
                     if quarantine is not None else None,
                     json.dumps(rows, ensure_ascii=False)
                     if rows is not None else None))
                conn.commit()
                return int(cur.lastrowid)
            finally:
                conn.close()

    def publish_batch(self, name: str, contract: dict[str, Any],
                      total_rows: int, rows: list) -> int:
        """全量通过的批次发布入库，绑定校验时的契约快照。"""
        return self._insert_batch(name, "published", contract, total_rows, 0,
                                  [], [], rows)

    def quarantine_batch(self, name: str, contract: dict[str, Any],
                         total_rows: int, error_rows: int,
                         header_errors: list, quarantine: list) -> int:
        """校验未通过的批次隔离留痕，不改变上次可用批次。"""
        return self._insert_batch(name, "quarantined", contract, total_rows,
                                  error_rows, header_errors, quarantine, None)

    # -- 批次读（每行自带契约快照，查询绝不混版） ---------------------------

    @staticmethod
    def _batch_row(row: sqlite3.Row, full: bool) -> dict[str, Any]:
        item = {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "state": row["state"],
            "total_rows": row["total_rows"],
            "error_rows": row["error_rows"],
            "contract": {
                "id": row["contract_id"],
                "version": row["contract_version"],
                "payload": json.loads(row["contract_payload"]),
            },
        }
        if full:
            item["header_errors"] = json.loads(row["header_errors"] or "[]")
            item["quarantine"] = json.loads(row["quarantine"] or "[]")
            item["rows"] = json.loads(row["rows_payload"]) if row["rows_payload"] else None
        return item

    def current_batch(self) -> Optional[dict[str, Any]]:
        """上次可用批次（最新发布成功的批次），含其绑定的契约快照。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM batches WHERE state='published' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            return self._batch_row(row, full=True) if row else None
        finally:
            conn.close()

    def get_batch(self, batch_id: int) -> Optional[dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM batches WHERE id=?",
                               (batch_id,)).fetchone()
            return self._batch_row(row, full=True) if row else None
        finally:
            conn.close()

    def batch_history(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at, state, contract_version, "
                "total_rows, error_rows FROM batches ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
