import mimetypes
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core.adobe_client import AuthError, QuotaExhaustedError, UpstreamTemporaryError
from core.request_logging import set_request_log_params
from core.s3_uploader import S3UploadError, S3Uploader


SEEDANCE_FAST_MODEL = "doubao-seedance-2-0-fast-260128"
SEEDANCE_STANDARD_MODEL = "doubao-seedance-2-0-260128"
SEEDANCE_MODEL_VERSIONS = {
    SEEDANCE_FAST_MODEL: "seedance_2.0_fast",
    SEEDANCE_STANDARD_MODEL: "seedance_2.0",
}
SEEDANCE_RATIOS = {"16:9", "9:16", "3:4", "4:3", "1:1"}
SEEDANCE_REFERENCE_MODE = "reference"
SEEDANCE_FRAME_MODE = "frame"
IMAGE_LIMIT_BYTES = 20 * 1024 * 1024
VIDEO_LIMIT_BYTES = 50 * 1024 * 1024
AUDIO_LIMIT_BYTES = 15 * 1024 * 1024


def build_seedance_router(
    *,
    store,
    token_manager,
    client,
    config_manager,
    generated_dir: Path,
    require_service_api_key: Callable[[Request], None],
    public_generated_url: Callable[[Request, str], str],
    on_generated_file_written: Callable[[Path, int, int], None],
    logger,
) -> APIRouter:
    router = APIRouter()
    data_dir = generated_dir.parent
    temp_root = data_dir / "tmp" / "seedance"
    temp_root.mkdir(parents=True, exist_ok=True)

    def _config_int(name: str, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(config_manager.get(name, default) or default)
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    slot_cond = threading.Condition()
    running_tasks = 0

    def _acquire_slot() -> None:
        nonlocal running_tasks
        with slot_cond:
            while running_tasks >= _config_int("seedance_max_concurrent", 2, 1, 20):
                slot_cond.wait(timeout=1.0)
            running_tasks += 1

    def _release_slot() -> None:
        nonlocal running_tasks
        with slot_cond:
            running_tasks = max(0, running_tasks - 1)
            slot_cond.notify_all()

    def _cors_headers(request: Request) -> dict[str, str]:
        origin = str(request.headers.get("origin") or "*").strip() or "*"
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key",
            "Access-Control-Max-Age": "86400",
        }

    def _json(request: Request, payload: dict[str, Any], status_code: int = 200):
        return JSONResponse(
            status_code=status_code,
            content=payload,
            headers=_cors_headers(request),
        )

    @router.options("/api/v3/contents/generations/tasks")
    def seedance_create_options(request: Request):
        return Response(status_code=204, headers=_cors_headers(request))

    @router.options("/api/v3/contents/generations/tasks/{task_id:path}")
    def seedance_status_options(task_id: str, request: Request):
        return Response(status_code=204, headers=_cors_headers(request))

    @router.post("/api/v3/contents/generations/tasks")
    def create_seedance_task(data: dict, request: Request):
        require_service_api_key(request)
        payload = dict(data or {})
        model = str(payload.get("model") or "").strip()
        if model not in SEEDANCE_MODEL_VERSIONS:
            return _json(
                request,
                {
                    "error": {
                        "code": "invalid_model",
                        "message": (
                            f"unsupported model: {model or '<empty>'}; supported: "
                            + ", ".join(SEEDANCE_MODEL_VERSIONS)
                        ),
                    }
                },
                status_code=400,
            )
        prompt = _prompt_from_payload(payload)
        if not prompt:
            return _json(
                request,
                {"error": {"code": "invalid_prompt", "message": "prompt is required"}},
                status_code=400,
            )
        try:
            duration = _duration_seconds(_first_present(payload, "duration", "seconds"))
            ratio = _normalize_ratio(_first_present(payload, "ratio", "aspect_ratio", "size"))
            resolution = _normalize_resolution(
                _first_present(payload, "resolution", "quality")
            )
            frame_image_urls = _collect_frame_image_urls(payload)
            image_urls = _collect_image_urls(payload)
            for url in frame_image_urls:
                _append_unique(image_urls, url)
            video_urls = _collect_video_urls(payload)
            audio_urls = _collect_audio_urls(payload)
            mode = _normalize_generation_mode(payload, frame_image_urls)
            _validate_seedance_request(
                duration=duration,
                ratio=ratio,
                resolution=resolution,
                mode=mode,
                image_urls=image_urls,
                video_urls=video_urls,
                audio_urls=audio_urls,
            )
            set_request_log_params(
                request,
                media_type="video",
                ratio=ratio,
                resolution=resolution,
                duration=duration,
                mode=mode,
                reference_count=len(image_urls) + len(video_urls) + len(audio_urls),
            )
        except ValueError as exc:
            return _json(
                request,
                {"error": {"code": "invalid_request", "message": str(exc)}},
                status_code=400,
            )

        task = store.create(
            model=model,
            prompt=prompt,
            duration=duration,
            ratio=ratio,
            resolution=resolution,
            image_urls=image_urls,
            video_urls=video_urls,
            audio_urls=audio_urls,
            original_request=payload,
        )
        threading.Thread(target=_run_task, args=(task.id,), daemon=True).start()
        return _json(
            request,
            _task_payload(
                task,
                request,
                public_generated_url,
                include_public_metadata=True,
            ),
        )

    @router.get("/api/v3/contents/generations/tasks/{task_id:path}")
    def get_seedance_task(task_id: str, request: Request):
        require_service_api_key(request)
        task = store.get(task_id)
        if task is None:
            return _json(
                request,
                {"error": {"code": "not_found", "message": "task not found"}},
                status_code=404,
            )
        return _json(
            request,
            _task_payload(
                task,
                request,
                public_generated_url,
                include_public_metadata=False,
            ),
        )

    def _run_task(task_id: str) -> None:
        _acquire_slot()
        try:
            task = store.get(task_id)
            if task is None:
                return
            work_dir = temp_root / task.id
            token = ""
            try:
                work_dir.mkdir(parents=True, exist_ok=True)
                store.update(task.id, status="uploading", progress=8.0)
                images = [
                    _download_media(url, "image", idx, work_dir)
                    for idx, url in enumerate(task.image_urls, start=1)
                ]
                videos = [
                    _download_media(url, "video", idx, work_dir)
                    for idx, url in enumerate(task.video_urls, start=1)
                ]
                audios = [
                    _download_media(url, "audio", idx, work_dir)
                    for idx, url in enumerate(task.audio_urls, start=1)
                ]
                _validate_downloaded_media(videos, audios)

                token = token_manager.get_available(
                    strategy=getattr(client, "token_rotation_strategy", "round_robin")
                ) or ""
                if not token:
                    raise RuntimeError("No active Firefly token available")

                uploaded = {"images": [], "videos": [], "audios": []}
                for item in images:
                    uploaded["images"].append(_upload_media_asset(token, item, "image"))
                for item in videos:
                    uploaded["videos"].append(_upload_media_asset(token, item, "video"))
                for item in audios:
                    uploaded["audios"].append(_upload_media_asset(token, item, "audio"))

                submit_prompt, reference_blobs = _build_reference_blobs(
                    task.prompt,
                    uploaded["images"],
                    uploaded["videos"],
                    uploaded["audios"],
                    image_mode=_normalize_generation_mode(
                        task.original_request,
                        _collect_frame_image_urls(task.original_request),
                    ),
                )
                upstream_request = client.build_seedance_video_payload(
                    prompt=submit_prompt,
                    model_version=SEEDANCE_MODEL_VERSIONS.get(
                        task.model, "seedance_2.0_fast"
                    ),
                    duration=task.duration,
                    aspect_ratio=task.ratio,
                    reference_blobs=reference_blobs,
                    negative_prompt=str(
                        task.original_request.get("negative_prompt")
                        or task.original_request.get("negativePrompt")
                        or ""
                    ),
                    generate_audio=_coerce_bool(
                        _first_present(
                            task.original_request,
                            "generate_audio",
                            "generateAudio",
                        ),
                        True,
                    ),
                )
                store.update(
                    task.id,
                    status="submitting",
                    progress=15.0,
                    uploaded_assets=uploaded,
                    upstream_request=upstream_request,
                )

                tmp_path = generated_dir / f"{task.id}.video.tmp"
                old_size = int(tmp_path.stat().st_size) if tmp_path.exists() else 0

                def progress_cb(update: dict) -> None:
                    upstream_task_id = str(update.get("upstream_job_id") or "").strip()
                    submit_response = update.get("submit_response")
                    status_response = update.get("status_response")
                    fields: dict[str, Any] = {
                        "status": "running",
                        "progress": float(update.get("task_progress") or 30.0),
                    }
                    if upstream_task_id:
                        fields["upstream_task_id"] = upstream_task_id
                    if isinstance(submit_response, dict):
                        fields["upstream_submit"] = submit_response
                    if isinstance(status_response, dict):
                        fields["upstream_status"] = status_response
                    store.update(task.id, **fields)

                video_bytes, status_data, submit_data = client.generate_seedance_video(
                    token=token,
                    payload=upstream_request,
                    timeout=_config_int("seedance_task_timeout_seconds", 900, 60, 7200),
                    out_path=tmp_path,
                    poll_interval=float(
                        _config_int("seedance_poll_interval_seconds", 3, 1, 60)
                    ),
                    progress_cb=progress_cb,
                )
                filename = f"{task.id}.mp4"
                out_path = generated_dir / filename
                if video_bytes is not None:
                    out_path.write_bytes(video_bytes)
                elif tmp_path.exists():
                    tmp_path.replace(out_path)
                if not out_path.exists() or out_path.stat().st_size <= 0:
                    raise RuntimeError("seedance video result was not written")
                new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                on_generated_file_written(out_path, old_size, new_size)
                token_manager.report_success(token)
                try:
                    result_video_url = _maybe_upload_result_video(out_path)
                except Exception as exc:
                    logger.exception("seedance S3 upload failed task_id=%s", task.id)
                    store.fail(task.id, str(exc), "S3_UPLOAD_FAILED")
                    return
                store.update(
                    task.id,
                    status="succeeded",
                    progress=100.0,
                    video_filename=filename,
                    video_url=result_video_url,
                    upstream_submit=submit_data if isinstance(submit_data, dict) else {},
                    upstream_status=status_data if isinstance(status_data, dict) else {},
                    completed_at=int(time.time()),
                    error="",
                    error_code="",
                )
            except AuthError as exc:
                if token:
                    token_manager.handle_auth_failure(token)
                store.fail(task_id, str(exc), "AUTHENTICATION_ERROR")
            except QuotaExhaustedError as exc:
                if token:
                    token_manager.report_exhausted(token)
                store.fail(task_id, str(exc), "QUOTA_EXHAUSTED")
            except UpstreamTemporaryError as exc:
                if token:
                    token_manager.report_error(token)
                store.fail(task_id, str(exc), "UPSTREAM_TEMPORARY_ERROR")
            except Exception as exc:
                if token:
                    token_manager.report_error(token)
                logger.exception("seedance task failed task_id=%s", task_id)
                store.fail(task_id, str(exc), "GENERATION_FAILED")
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        finally:
            _release_slot()

    def _upload_media_asset(token: str, item: dict[str, Any], kind: str) -> dict[str, Any]:
        path = Path(str(item.get("path") or ""))
        asset_id = client.upload_storage_asset(
            token,
            kind,
            path.read_bytes(),
            str(item.get("mime") or "application/octet-stream"),
        )
        return {
            "id": asset_id,
            "source_url": item.get("url"),
            "mime": item.get("mime"),
            "size": item.get("size"),
            "duration": item.get("duration"),
        }

    def _maybe_upload_result_video(path: Path) -> str:
        uploader = S3Uploader(config_manager.get_all())
        if not uploader.is_enabled():
            return ""
        try:
            return uploader.upload_file(path, content_type="video/mp4")
        except S3UploadError:
            raise
        except Exception as exc:
            raise S3UploadError(str(exc)) from exc

    def _download_media(
        url: str, kind: str, index: int, work_dir: Path
    ) -> dict[str, Any]:
        clean_url = str(url or "").strip()
        limit = {
            "image": IMAGE_LIMIT_BYTES,
            "video": VIDEO_LIMIT_BYTES,
            "audio": AUDIO_LIMIT_BYTES,
        }[kind]
        timeout = _config_int("seedance_media_download_timeout_seconds", 60, 5, 600)
        parsed = urlparse(clean_url)
        suffix = Path(parsed.path).suffix
        if not suffix:
            suffix = {"image": ".jpg", "video": ".mp4", "audio": ".mp3"}[kind]
        out_path = work_dir / f"{kind}_{index}{suffix}"
        try:
            with requests.get(
                clean_url,
                stream=True,
                timeout=timeout,
                proxies=client._requests_proxies(),
            ) as resp:
                if resp.status_code != 200:
                    raise ValueError(f"download {kind} failed: HTTP {resp.status_code}")
                length = resp.headers.get("content-length")
                if length:
                    try:
                        length_value = int(length)
                    except Exception:
                        length_value = 0
                    if length_value > limit:
                        raise ValueError(f"{kind} too large")
                total = 0
                with out_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise ValueError(f"{kind} too large")
                        f.write(chunk)
                content_type = str(resp.headers.get("content-type") or "").split(";")[0]
        except requests.RequestException as exc:
            raise ValueError(f"download {kind} failed: {exc}")
        mime = _normalize_mime(kind, content_type, out_path)
        result: dict[str, Any] = {
            "url": clean_url,
            "path": str(out_path),
            "mime": mime,
            "size": int(out_path.stat().st_size),
        }
        if kind in {"video", "audio"}:
            result["duration"] = _probe_duration(out_path)
        return result

    return router


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _duration_seconds(value: Any) -> int:
    if value is None or value == "":
        return 5
    if isinstance(value, (int, float)):
        duration = int(float(value))
    else:
        text = str(value).strip().lower()
        text = text.removeprefix("~").strip()
        text = text.removesuffix("seconds").removesuffix("second").removesuffix("secs")
        text = text.removesuffix("sec").removesuffix("s").strip()
        duration = int(float(text))
    if duration < 4 or duration > 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    return duration


def _normalize_ratio(value: Any) -> str:
    ratio = _string_value(value) or "16:9"
    if "x" in ratio.lower() and ":" not in ratio:
        parts = re.split(r"[xX]", ratio)
        if len(parts) == 2:
            try:
                w = int(float(parts[0]))
                h = int(float(parts[1]))
                known = {
                    (1280, 720): "16:9",
                    (720, 1280): "9:16",
                    (960, 720): "4:3",
                    (720, 960): "3:4",
                    (720, 720): "1:1",
                }
                ratio = known.get((w, h), ratio)
            except Exception:
                pass
    if ratio not in SEEDANCE_RATIOS:
        raise ValueError("ratio must be one of 16:9, 9:16, 3:4, 4:3, 1:1")
    return ratio


def _normalize_resolution(value: Any) -> str:
    resolution = (_string_value(value) or "720p").lower()
    if resolution == "720":
        resolution = "720p"
    if resolution != "720p":
        raise ValueError("resolution must be 720p")
    return resolution


def _normalize_generation_mode(
    payload: dict[str, Any],
    frame_image_urls: list[str] | None = None,
) -> str:
    raw = _first_present(
        payload,
        "mode",
        "generation_mode",
        "generationMode",
        "generation_type",
        "generationType",
    )
    if raw is None and frame_image_urls:
        return SEEDANCE_FRAME_MODE
    if raw is None:
        return SEEDANCE_REFERENCE_MODE
    if isinstance(raw, (int, float)):
        value = int(raw)
        if value in {0, 2}:
            return SEEDANCE_FRAME_MODE
        return SEEDANCE_REFERENCE_MODE
    text = str(raw or "").strip().lower().replace("-", "_")
    frame_aliases = {
        "0",
        "2",
        "i2v",
        "image2video",
        "image_to_video",
        "first_frame",
        "first_last",
        "first_last_frame",
        "start_end",
        "start_end_frame",
        "i2v_first_last",
        "keyframe",
        "keyframes",
        "keyframes_to_video",
    }
    if text in frame_aliases:
        return SEEDANCE_FRAME_MODE
    return SEEDANCE_REFERENCE_MODE


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _validate_seedance_request(
    *,
    duration: int,
    ratio: str,
    resolution: str,
    mode: str = SEEDANCE_REFERENCE_MODE,
    image_urls: list[str],
    video_urls: list[str],
    audio_urls: list[str],
) -> None:
    if duration < 4 or duration > 15:
        raise ValueError("duration must be between 4 and 15 seconds")
    if ratio not in SEEDANCE_RATIOS:
        raise ValueError("unsupported ratio")
    if resolution != "720p":
        raise ValueError("resolution must be 720p")
    if mode == SEEDANCE_FRAME_MODE:
        if len(image_urls) < 1 or len(image_urls) > 2:
            raise ValueError("first/end frame mode requires 1 or 2 image URLs")
    elif len(image_urls) > 9:
        raise ValueError("at most 9 image_urls are supported")
    if len(video_urls) > 1:
        raise ValueError("at most 1 video_urls item is supported")
    if len(audio_urls) > 3:
        raise ValueError("at most 3 audio_urls are supported")
    for url in image_urls + video_urls + audio_urls:
        if not _is_http_url(url):
            raise ValueError("media URLs must be http or https")


def _prompt_from_payload(payload: dict[str, Any]) -> str:
    direct = _string_value(payload.get("prompt") or payload.get("text"))
    if direct:
        return direct
    parts: list[str] = []
    for key in ("content", "input", "messages"):
        _collect_text(payload.get(key), parts)
    return "\n".join(parts).strip()


def _collect_text(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, out)
        return
    if isinstance(value, dict):
        text = _string_value(value.get("text"))
        if text:
            out.append(text)
            return
        for key in ("content", "input", "message"):
            _collect_text(value.get(key), out)


def _collect_image_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for value in _first_string_list(
        payload,
        "image_urls",
        "images",
        "reference_images",
        "img_url_list",
    ):
        _append_unique(urls, value)
    for key in (
        "image",
        "image_url",
        "first_frame_url",
        "first_image_url",
        "start_frame_url",
        "start_image_url",
        "last_frame_url",
        "last_image_url",
        "end_frame_url",
        "end_image_url",
        "reference_image",
    ):
        _collect_media_urls(payload.get(key), "image_url", urls)
    for key in ("content", "input", "messages"):
        _collect_media_urls(payload.get(key), "image_url", urls)
    return urls


def _collect_frame_image_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    first_keys = (
        "first_frame_url",
        "first_image_url",
        "start_frame_url",
        "start_image_url",
        "first_frame",
        "start_frame",
    )
    last_keys = (
        "last_frame_url",
        "last_image_url",
        "end_frame_url",
        "end_image_url",
        "last_frame",
        "end_frame",
    )
    for key in first_keys:
        _collect_media_urls(payload.get(key), "image_url", urls)
    for key in last_keys:
        _collect_media_urls(payload.get(key), "image_url", urls)
    for key in ("content", "input", "messages"):
        _collect_frame_role_image_urls(payload.get(key), urls)
    return urls


def _collect_frame_role_image_urls(value: Any, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            _collect_frame_role_image_urls(item, out)
        return
    if not isinstance(value, dict):
        return
    role_text = " ".join(
        str(value.get(key) or "").strip().lower().replace("-", "_")
        for key in ("role", "type", "name")
    )
    if any(
        marker in role_text
        for marker in (
            "first_frame",
            "start_frame",
            "last_frame",
            "end_frame",
            "keyframe",
        )
    ):
        _collect_media_urls(value, "image_url", out)
        return
    for key in ("content", "input", "message"):
        _collect_frame_role_image_urls(value.get(key), out)


def _collect_video_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for value in _first_string_list(payload, "video_urls", "reference_videos", "video_url_list"):
        _append_unique(urls, value)
    for key in ("video", "video_url", "reference_video"):
        _collect_media_urls(payload.get(key), "video_url", urls)
    for key in ("content", "input", "messages"):
        _collect_media_urls(payload.get(key), "video_url", urls)
    return urls


def _collect_audio_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for value in _first_string_list(
        payload, "audio_urls", "reference_audio", "reference_audios", "audio_url_list"
    ):
        _append_unique(urls, value)
    for key in ("audio", "audio_url", "reference_audio"):
        _collect_media_urls(payload.get(key), "audio_url", urls)
    for key in ("content", "input", "messages"):
        _collect_media_urls(payload.get(key), "audio_url", urls)
    return urls


def _first_string_list(payload: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        values = _string_list(payload.get(key))
        if values:
            return values
    return []


def _string_list(value: Any) -> list[str]:
    out: list[str] = []
    _collect_string_values(value, out)
    return out


def _collect_string_values(value: Any, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
        return
    if isinstance(value, list):
        for item in value:
            _collect_string_values(item, out)
        return
    if isinstance(value, dict):
        for key in ("url", "image_url", "video_url", "audio_url"):
            if key in value:
                _collect_string_values(value.get(key), out)


def _collect_media_urls(value: Any, media_key: str, out: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        _append_unique(out, value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_media_urls(item, media_key, out)
        return
    if isinstance(value, dict):
        if media_key in value:
            _collect_media_urls(value.get(media_key), media_key, out)
            return
        nested = value.get("url")
        if nested and str(value.get("type") or "").strip() in {"", media_key}:
            _collect_media_urls(nested, media_key, out)
            return
        for key in ("content", "input", "message"):
            _collect_media_urls(value.get(key), media_key, out)


def _append_unique(out: list[str], value: Any) -> None:
    text = _string_value(value)
    if text and text not in out:
        out.append(text)


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_mime(kind: str, header_mime: str, path: Path) -> str:
    mime = str(header_mime or "").strip().lower()
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(str(path))
        mime = str(guessed or "").lower()
    if not mime:
        return {
            "image": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/mpeg",
        }[kind]
    return mime


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        raise ValueError("ffprobe is required for video/audio duration validation")
    except subprocess.SubprocessError as exc:
        raise ValueError(f"failed to inspect media duration: {exc}")
    try:
        return float(str(result.stdout or "").strip())
    except Exception:
        raise ValueError("failed to inspect media duration")


def _validate_downloaded_media(videos: list[dict[str, Any]], audios: list[dict[str, Any]]) -> None:
    for item in videos:
        duration = float(item.get("duration") or 0)
        if duration < 2.0 or duration > 15.2:
            raise ValueError("video duration must be between 2 and 15 seconds")
    total_audio = 0.0
    for item in audios:
        duration = float(item.get("duration") or 0)
        if duration < 2.0 or duration > 15.2:
            raise ValueError("audio duration must be between 2 and 15 seconds")
        total_audio += duration
    if total_audio > 15.2:
        raise ValueError("audio total duration must not exceed 15 seconds")


def _build_reference_blobs(
    prompt: str,
    images: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    audios: list[dict[str, Any]],
    image_mode: str = SEEDANCE_REFERENCE_MODE,
) -> tuple[str, list[dict[str, Any]]]:
    refs: list[dict[str, Any]] = []
    used_mentions: set[str] = set()
    next_prompt = str(prompt or "")

    def new_mention_id() -> str:
        while True:
            value = secrets.token_urlsafe(18)[:21]
            if value not in used_mentions:
                used_mentions.add(value)
                return value

    def replace_aliases(text: str, aliases: list[str], mention_id: str) -> str:
        out = text
        for alias in sorted(aliases, key=len, reverse=True):
            out = out.replace(alias, f"@{mention_id}")
        return out

    if image_mode == SEEDANCE_FRAME_MODE:
        for idx, item in enumerate(images[:2], start=1):
            refs.append(
                {
                    "id": str(item.get("id") or ""),
                    "usage": "frame",
                    "order": idx,
                }
            )
        images = []

    for idx, item in enumerate(images, start=1):
        mention_id = new_mention_id()
        next_prompt = replace_aliases(
            next_prompt,
            [f"@图片{idx}", f"@图{idx}", f"@Image{idx}", f"@image{idx}"],
            mention_id,
        )
        refs.append(
            {
                "id": str(item.get("id") or ""),
                "usage": "style",
                "mention": {"id": mention_id, "label": f"Image{idx}"},
            }
        )
    for idx, item in enumerate(videos, start=1):
        mention_id = new_mention_id()
        next_prompt = replace_aliases(
            next_prompt,
            [f"@视频{idx}", f"@Video{idx}", f"@video{idx}"],
            mention_id,
        )
        refs.append(
            {
                "id": str(item.get("id") or ""),
                "usage": "source",
                "mention": {"id": mention_id, "label": f"Video{idx}"},
            }
        )
    for idx, item in enumerate(audios, start=1):
        mention_id = new_mention_id()
        next_prompt = replace_aliases(
            next_prompt,
            [f"@音频{idx}", f"@Audio{idx}", f"@audio{idx}"],
            mention_id,
        )
        refs.append(
            {
                "id": str(item.get("id") or ""),
                "usage": "source",
                "mention": {"id": mention_id, "label": f"Audio{idx}"},
            }
        )
    return next_prompt, refs


def _task_payload(
    task,
    request: Request,
    public_generated_url: Callable[[Request, str], str],
    *,
    include_public_metadata: bool = False,
) -> dict[str, Any]:
    status = _public_status(task.status)
    video_url = ""
    if status == "succeeded":
        video_url = str(getattr(task, "video_url", "") or "").strip()
        if not video_url and task.video_filename:
            video_url = public_generated_url(request, task.video_filename)
    error_payload = None
    if status == "failed":
        error_payload = {
            "code": task.error_code or "generation_failed",
            "message": task.error or "generation failed",
        }
    item: dict[str, Any] = {
        "id": task.id,
        "status": status,
        "progress": task.progress,
        "content": {"video_url": video_url},
    }
    if video_url:
        item["video_url"] = video_url
    if error_payload:
        item["error"] = error_payload["message"]
    payload: dict[str, Any] = {
        "id": task.id,
        "task_id": task.id,
        "upstream_task_id": task.upstream_task_id,
        "model": task.model,
        "status": status,
        "progress": task.progress,
        "content": {"video_url": video_url},
        "video_url": video_url,
        "error": error_payload,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "items": [item],
    }
    if include_public_metadata:
        payload["helper_task_id"] = task.id
        payload["provider"] = "firefly"
    if task.completed_at:
        payload["completed_at"] = task.completed_at
    return payload


def _public_status(status: str) -> str:
    if status == "queued":
        return "queued"
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    return "running"
