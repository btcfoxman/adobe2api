from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ResponseTaskRecord:
    id: str
    response_id: str
    prompt: str
    model: str
    aspect_ratio: str
    output_resolution: str
    status: str = "queued"
    progress: float = 0.0
    result_urls: list[str] = field(default_factory=list)
    input_image_count: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    upstream_job_id: str = ""
    error: str = ""
    created_at: int = 0
    updated_at: int = 0
    completed_at: Optional[int] = None


class ResponseTaskStore:
    """SQLite-backed Responses image task store safe for threaded workers."""

    terminal_statuses = {"succeeded", "failed"}
    _json_columns = {"result_urls", "context"}
    _update_columns = {
        "status",
        "progress",
        "result_urls",
        "upstream_job_id",
        "error",
        "completed_at",
    }

    def __init__(self, file_path: Path, max_items: int = 5000) -> None:
        self._file_path = Path(file_path)
        self._max_items = max(100, int(max_items or 5000))
        self._lock = threading.RLock()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._file_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_tasks (
                    id TEXT PRIMARY KEY,
                    response_id TEXT NOT NULL UNIQUE,
                    prompt TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    aspect_ratio TEXT NOT NULL DEFAULT '',
                    output_resolution TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    result_urls_json TEXT NOT NULL DEFAULT '[]',
                    input_image_count INTEGER NOT NULL DEFAULT 0,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    upstream_job_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    completed_at INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_response_tasks_created ON response_tasks(created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_response_tasks_status ON response_tasks(status)"
            )
            now = int(time.time())
            conn.execute(
                """
                UPDATE response_tasks
                SET status='failed', progress=0,
                    error='Task interrupted by service restart',
                    updated_at=?, completed_at=COALESCE(completed_at, ?)
                WHERE status NOT IN ('succeeded', 'failed')
                """,
                (now, now),
            )

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        try:
            return json.loads(str(value)) if value not in (None, "") else default
        except Exception:
            return default

    @classmethod
    def _from_row(cls, row: sqlite3.Row | None) -> Optional[ResponseTaskRecord]:
        if row is None:
            return None
        data = dict(row)
        data["result_urls"] = cls._loads(data.pop("result_urls_json", "[]"), [])
        data["context"] = cls._loads(data.pop("context_json", "{}"), {})
        return ResponseTaskRecord(**data)

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        count = int(conn.execute("SELECT COUNT(*) FROM response_tasks").fetchone()[0])
        overflow = count - self._max_items + 1
        if overflow <= 0:
            return
        conn.execute(
            """
            DELETE FROM response_tasks WHERE id IN (
                SELECT id FROM response_tasks
                WHERE status IN ('succeeded', 'failed')
                ORDER BY created_at ASC LIMIT ?
            )
            """,
            (overflow,),
        )

    def create(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        output_resolution: str,
        input_image_count: int,
        context: dict[str, Any],
    ) -> ResponseTaskRecord:
        now = int(time.time())
        item = ResponseTaskRecord(
            id=f"imgtask_{uuid.uuid4().hex}",
            response_id=f"resp_{uuid.uuid4().hex}",
            prompt=str(prompt),
            model=str(model),
            aspect_ratio=str(aspect_ratio),
            output_resolution=str(output_resolution),
            input_image_count=max(0, int(input_image_count or 0)),
            context=dict(context or {}),
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._connect() as conn:
            self._prune_locked(conn)
            conn.execute(
                """
                INSERT INTO response_tasks(
                    id, response_id, prompt, model, aspect_ratio, output_resolution,
                    status, progress, result_urls_json, input_image_count, context_json,
                    upstream_job_id, error, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.response_id,
                    item.prompt,
                    item.model,
                    item.aspect_ratio,
                    item.output_resolution,
                    item.status,
                    item.progress,
                    "[]",
                    item.input_image_count,
                    json.dumps(item.context, ensure_ascii=False, separators=(",", ":")),
                    "",
                    "",
                    now,
                    now,
                    None,
                ),
            )
        return item

    def get(self, task_id_or_response_id: str) -> Optional[ResponseTaskRecord]:
        target = str(task_id_or_response_id or "").strip()
        if not target:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM response_tasks WHERE id=? OR response_id=? LIMIT 1",
                (target, target),
            ).fetchone()
        return self._from_row(row)

    def update(self, task_id: str, **changes: Any) -> Optional[ResponseTaskRecord]:
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in changes.items():
            if key not in self._update_columns:
                continue
            column = f"{key}_json" if key in self._json_columns else key
            if key in self._json_columns:
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            assignments.append(f"{column}=?")
            values.append(value)
        if not assignments:
            return self.get(task_id)
        assignments.append("updated_at=?")
        values.append(int(time.time()))
        values.append(str(task_id))
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE response_tasks SET {', '.join(assignments)} WHERE id=?",
                values,
            )
        return self.get(task_id)

    def as_dict(self, task_id_or_response_id: str) -> Optional[dict[str, Any]]:
        item = self.get(task_id_or_response_id)
        return asdict(item) if item else None
