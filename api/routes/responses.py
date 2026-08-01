from __future__ import annotations

import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from api.schemas import ResponsesCreateRequest
from core.request_logging import set_request_log_params


def _text(value: Any) -> str:
    return str(value or "").strip()


def prompt_from_responses_input(payload: ResponsesCreateRequest) -> str:
    if _text(payload.prompt):
        return _text(payload.prompt)
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("type") in {"input_text", "text"}:
                text = _text(value.get("text"))
                if text:
                    parts.append(text)
            elif "content" in value:
                walk(value.get("content"))

    walk(payload.input)
    return "\n".join(item for item in parts if item).strip()


def _image_url_from_part(part: Any) -> str:
    if isinstance(part, str):
        return _text(part)
    if not isinstance(part, dict):
        return ""
    value = part.get("image_url")
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        return _text(value.get("url") or value.get("image_url"))
    return _text(part.get("url") or part.get("src"))


def image_urls_from_responses_input(payload: ResponsesCreateRequest) -> list[str]:
    collected: list[str] = []

    def add(value: Any) -> None:
        url = _image_url_from_part(value)
        if url and url not in collected:
            collected.append(url)

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("type") in {"input_image", "image_url"}:
                add(value)
            if isinstance(value.get("content"), (list, dict)):
                walk(value.get("content"))

    for item in payload.image_urls or []:
        add(item)
    metadata = payload.metadata or {}
    metadata_images = metadata.get("image_urls") or []
    for item in metadata_images if isinstance(metadata_images, list) else [metadata_images]:
        add(item)
    walk(payload.input)
    return collected


def _first_text(*values: Any) -> str:
    for value in values:
        result = _text(value)
        if result:
            return result
    return ""


def _image_tool(payload: ResponsesCreateRequest) -> dict[str, Any]:
    for tool in payload.tools or []:
        if isinstance(tool, dict) and _text(tool.get("type")) == "image_generation":
            return tool
    return {}


def _output_type(payload: ResponsesCreateRequest | None = None, context: dict | None = None) -> str:
    if context and _text(context.get("output_type")):
        return _text(context.get("output_type"))
    tool_type = ""
    for tool in (payload.tools if payload else None) or []:
        if isinstance(tool, dict) and _text(tool.get("type")):
            tool_type = _text(tool.get("type"))
            break
    if not tool_type:
        return "image_generation_call"
    return tool_type if tool_type.endswith("_call") else f"{tool_type}_call"


def responses_context_from_payload(payload: ResponsesCreateRequest, model: str) -> dict[str, Any]:
    return {
        "output_type": _output_type(payload),
        "model": model,
        "instructions": payload.instructions,
        "metadata": dict(payload.metadata or {}),
        "parallel_tool_calls": payload.parallel_tool_calls if payload.parallel_tool_calls is not None else True,
        "temperature": payload.temperature,
        "tool_choice": payload.tool_choice,
        "top_p": payload.top_p,
        "max_output_tokens": payload.max_output_tokens,
        "previous_response_id": payload.previous_response_id,
        "reasoning": payload.reasoning,
        "truncation": payload.truncation,
        "user": payload.user,
        "store": payload.store,
    }


def response_from_task(task: Any) -> dict[str, Any]:
    if isinstance(task, dict):
        data = dict(task)
    elif is_dataclass(task):
        data = asdict(task)
    else:
        data = dict(vars(task))
    context = dict(data.get("context") or {})
    status = {
        "queued": "in_progress",
        "running": "in_progress",
        "succeeded": "completed",
        "failed": "failed",
    }.get(_text(data.get("status")), "in_progress")
    output = [
        {
            "id": f"ig_{data.get('id')}_{index}",
            "result": None,
            "status": status,
            "type": _output_type(context=context),
            "url": url,
        }
        for index, url in enumerate(data.get("result_urls") or [])
    ]
    metadata = dict(context.get("metadata") or {})
    metadata.setdefault("task_id", data.get("id"))
    if data.get("upstream_job_id"):
        metadata.setdefault("upstream_job_id", data.get("upstream_job_id"))
    error = {"message": _text(data.get("error"))} if data.get("error") else None
    return {
        "id": data.get("response_id") or data.get("id"),
        "created_at": int(data.get("created_at") or 0),
        "error": error,
        "incomplete_details": None,
        "instructions": context.get("instructions"),
        "metadata": metadata,
        "model": context.get("model") or data.get("model"),
        "object": "response",
        "output": output,
        "parallel_tool_calls": context.get("parallel_tool_calls") if context.get("parallel_tool_calls") is not None else True,
        "temperature": context.get("temperature"),
        "tool_choice": context.get("tool_choice"),
        "tools": None,
        "top_p": context.get("top_p"),
        "max_output_tokens": context.get("max_output_tokens"),
        "previous_response_id": context.get("previous_response_id"),
        "reasoning": context.get("reasoning"),
        "status": status,
        "text": None,
        "truncation": context.get("truncation"),
        "usage": None,
        "user": context.get("user"),
        "store": context.get("store"),
    }


def build_responses_router(
    *,
    store,
    token_manager,
    client,
    generated_dir: Path,
    video_model_catalog: dict,
    default_model_id: str,
    resolve_model: Callable[[str | None], dict],
    resolve_ratio_and_resolution: Callable[[dict, str | None], tuple[str, str, str]],
    require_service_api_key: Callable[[Request], None],
    public_image_url: Callable[[Request, str], str],
    load_input_images: Callable[[Any], list[tuple[bytes, str]]],
    on_generated_file_written: Callable[[Path, int, int], None],
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    logger,
) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/responses")
    def create_response(data: ResponsesCreateRequest, request: Request):
        require_service_api_key(request)
        prompt = prompt_from_responses_input(data)
        if not prompt:
            raise HTTPException(status_code=422, detail="responses image task requires prompt/input text")
        if data.n != 1:
            raise HTTPException(status_code=400, detail="adobe2api responses currently supports n=1")

        metadata = data.metadata or {}
        tool = _image_tool(data)
        requested_model = _first_text(data.model, metadata.get("model"))
        if requested_model in video_model_catalog:
            raise HTTPException(status_code=400, detail="/v1/responses only supports image models")
        request_ratio = _first_text(
            data.ratio,
            data.aspect_ratio,
            metadata.get("ratio"),
            metadata.get("aspect_ratio"),
            tool.get("ratio"),
            tool.get("aspect_ratio"),
        )
        request_size = _first_text(data.size, metadata.get("size"), tool.get("size"))
        request_resolution = _first_text(
            data.resolution, metadata.get("resolution"), tool.get("resolution")
        )
        options = {
            "aspect_ratio": request_ratio,
            "size": request_size,
            "resolution": request_resolution,
            "quality": data.quality,
        }
        ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
            options, requested_model or None
        )
        set_request_log_params(
            request,
            media_type="image",
            size=request_size,
            ratio=ratio,
            resolution=output_resolution,
            quality=data.quality,
            n=data.n,
        )
        model_conf = resolve_model(resolved_model_id)
        image_urls = image_urls_from_responses_input(data)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}}
                    for url in image_urls
                ],
            }
        ]
        input_images = load_input_images(messages) if image_urls else []
        public_model_id = requested_model or resolved_model_id or default_model_id
        context = responses_context_from_payload(data, public_model_id)
        task = store.create(
            prompt=prompt,
            model=resolved_model_id or default_model_id,
            aspect_ratio=ratio,
            output_resolution=output_resolution,
            input_image_count=len(input_images),
            context=context,
        )
        image_url = public_image_url(request, task.id)
        initial_response = response_from_task(task)

        def runner() -> None:
            store.update(task.id, status="running", progress=5.0)
            max_attempts = client.retry_max_attempts if client.retry_enabled else 1
            max_attempts = max(1, int(max_attempts))
            last_error = "No active tokens available in the pool"
            for attempt in range(1, max_attempts + 1):
                token = token_manager.get_available(strategy=client.token_rotation_strategy)
                if not token:
                    break
                try:
                    source_image_ids = [
                        client.upload_image(token, image_bytes, image_mime or "image/jpeg")
                        for image_bytes, image_mime in input_images
                    ]

                    def progress_cb(update: dict) -> None:
                        state = _text(update.get("task_status")).upper()
                        status = "failed" if state == "FAILED" else "running"
                        store.update(
                            task.id,
                            status=status,
                            progress=float(update.get("task_progress") or 0.0),
                            upstream_job_id=_text(update.get("upstream_job_id")),
                            error=_text(update.get("error")),
                        )

                    out_path = generated_dir / f"{task.id}.png"
                    old_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    image_bytes, meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=_text(model_conf.get("upstream_model_id")) or "gemini-flash",
                        upstream_model_version=_text(model_conf.get("upstream_model_version")) or "nano-banana-2",
                        quality_level=(client.gpt_image_quality if _text(model_conf.get("upstream_model_id")) == "gpt-image" else None),
                        detail_level=model_conf.get("detail_level"),
                        source_image_ids=source_image_ids,
                        timeout=client.generate_timeout,
                        out_path=out_path,
                        progress_cb=progress_cb,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    progress = float((meta or {}).get("progress") or 100.0)
                    store.update(
                        task.id,
                        status="succeeded",
                        progress=max(progress, 100.0),
                        result_urls=[image_url],
                        error="",
                        completed_at=int(time.time()),
                    )
                    return
                except quota_error_cls:
                    token_manager.report_exhausted(token)
                    last_error = "Token quota exhausted."
                    retryable = attempt < max_attempts
                except auth_error_cls:
                    token_manager.report_invalid(token)
                    last_error = "Token invalid or expired."
                    retryable = attempt < max_attempts
                except upstream_temp_error_cls as exc:
                    last_error = str(exc)
                    retryable = attempt < max_attempts and client.should_retry_temporary_error(exc)
                except Exception as exc:
                    logger.exception("Unhandled error in asynchronous /v1/responses task=%s", task.id)
                    store.update(
                        task.id,
                        status="failed",
                        progress=0.0,
                        error=str(exc),
                        completed_at=int(time.time()),
                    )
                    return
                if retryable:
                    delay = client._retry_delay_for_attempt(attempt)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break
            store.update(
                task.id,
                status="failed",
                progress=0.0,
                error=last_error,
                completed_at=int(time.time()),
            )

        threading.Thread(target=runner, name=f"responses-{task.id[:20]}", daemon=True).start()
        return initial_response

    @router.get("/v1/responses/{response_id}")
    def get_response(response_id: str, request: Request):
        require_service_api_key(request)
        task = store.get(response_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return response_from_task(task)

    return router
