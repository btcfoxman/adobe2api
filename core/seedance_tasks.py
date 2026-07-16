import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class SeedanceTaskRecord:
    id: str
    model: str
    prompt: str
    duration: int
    ratio: str
    resolution: str
    provider: str = "firefly"
    status: str = "queued"
    progress: float = 0.0
    image_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    audio_urls: list[str] = field(default_factory=list)
    original_request: dict[str, Any] = field(default_factory=dict)
    uploaded_assets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    upstream_request: dict[str, Any] = field(default_factory=dict)
    upstream_submit: dict[str, Any] = field(default_factory=dict)
    upstream_status: dict[str, Any] = field(default_factory=dict)
    upstream_task_id: str = ""
    video_filename: str = ""
    video_url: str = ""
    error: str = ""
    error_code: str = ""
    created_at: int = 0
    updated_at: int = 0
    completed_at: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeedanceTaskRecord":
        allowed = set(cls.__dataclass_fields__.keys())
        clean = {k: v for k, v in dict(data or {}).items() if k in allowed}
        item = cls(**clean)
        item.image_urls = [str(x) for x in (item.image_urls or [])]
        item.video_urls = [str(x) for x in (item.video_urls or [])]
        item.audio_urls = [str(x) for x in (item.audio_urls or [])]
        item.original_request = dict(item.original_request or {})
        item.uploaded_assets = dict(item.uploaded_assets or {})
        item.upstream_request = dict(item.upstream_request or {})
        item.upstream_submit = dict(item.upstream_submit or {})
        item.upstream_status = dict(item.upstream_status or {})
        return item


class SeedanceTaskStore:
    terminal_statuses = {"succeeded", "failed"}

    def __init__(self, file_path: Path, max_items: int = 500) -> None:
        self._file_path = file_path
        self._max_items = max(100, int(max_items or 500))
        self._lock = threading.Lock()
        self._items: dict[str, SeedanceTaskRecord] = {}
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self._file_path.exists():
            return
        try:
            raw = json.loads(self._file_path.read_text(encoding="utf-8"))
        except Exception:
            return
        items = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return
        now = int(time.time())
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                record = SeedanceTaskRecord.from_dict(item)
            except Exception:
                continue
            if record.status not in self.terminal_statuses:
                record.status = "failed"
                record.progress = 0.0
                record.error = "Task interrupted by service restart"
                record.error_code = "TASK_INTERRUPTED"
                record.completed_at = record.completed_at or now
                record.updated_at = now
                changed = True
            self._items[record.id] = record
        if changed:
            self._save_locked()

    def _save_locked(self) -> None:
        items = sorted(self._items.values(), key=lambda x: x.created_at)
        if len(items) > self._max_items:
            for old in items[: len(items) - self._max_items]:
                self._items.pop(old.id, None)
            items = sorted(self._items.values(), key=lambda x: x.created_at)
        payload = {"items": [asdict(item) for item in items]}
        tmp_path = self._file_path.with_suffix(self._file_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self._file_path)

    def create(
        self,
        *,
        model: str,
        prompt: str,
        duration: int,
        ratio: str,
        resolution: str,
        image_urls: list[str],
        video_urls: list[str],
        audio_urls: list[str],
        original_request: dict[str, Any],
    ) -> SeedanceTaskRecord:
        now = int(time.time())
        record = SeedanceTaskRecord(
            id=f"task_{uuid.uuid4().hex}",
            model=model,
            prompt=prompt,
            duration=duration,
            ratio=ratio,
            resolution=resolution,
            image_urls=list(image_urls or []),
            video_urls=list(video_urls or []),
            audio_urls=list(audio_urls or []),
            original_request=dict(original_request or {}),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._items[record.id] = record
            self._save_locked()
        return record

    def get(self, task_id: str) -> Optional[SeedanceTaskRecord]:
        tid = str(task_id or "").strip()
        with self._lock:
            item = self._items.get(tid)
            if item is None:
                return None
            return SeedanceTaskRecord.from_dict(asdict(item))

    def update(self, task_id: str, **kwargs: Any) -> Optional[SeedanceTaskRecord]:
        tid = str(task_id or "").strip()
        with self._lock:
            item = self._items.get(tid)
            if item is None:
                return None
            for key, value in kwargs.items():
                if key in SeedanceTaskRecord.__dataclass_fields__:
                    setattr(item, key, value)
            item.updated_at = int(time.time())
            self._save_locked()
            return SeedanceTaskRecord.from_dict(asdict(item))

    def fail(self, task_id: str, message: str, code: str = "GENERATION_FAILED") -> None:
        now = int(time.time())
        self.update(
            task_id,
            status="failed",
            progress=0.0,
            error=str(message or "generation failed"),
            error_code=str(code or "GENERATION_FAILED"),
            completed_at=now,
        )
