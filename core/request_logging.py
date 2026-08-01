from __future__ import annotations

import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlparse


MAX_REFERENCE_ASSETS = 12

_REQUEST_OPERATIONS = {
    ("POST", "/v1/chat/completions"): "chat.completions",
    ("POST", "/v1/images/generations"): "images.generations",
    ("POST", "/v1/images/edits"): "images.edits",
    ("POST", "/v1/responses"): "responses.create",
    ("POST", "/api/v1/generate"): "api.generate",
    (
        "POST",
        "/api/v3/contents/generations/tasks",
    ): "contents.generations.create",
    # Entity creation was already present in request logs and remains visible.
    ("POST", "/v1/entities"): "entities.create",
}

_ASSET_KEYS = {
    "image": "image",
    "images": "image",
    "image_url": "image",
    "image_urls": "image",
    "image_reference": "image",
    "image_references": "image",
    "reference_image": "image",
    "reference_images": "image",
    "input_image": "image",
    "input_images": "image",
    "input_reference": "image",
    "input_references": "image",
    "first_frame": "image",
    "last_frame": "image",
    "first_frame_image": "image",
    "last_frame_image": "image",
    "start_image": "image",
    "end_image": "image",
    "video": "video",
    "videos": "video",
    "video_url": "video",
    "video_urls": "video",
    "reference_video": "video",
    "reference_videos": "video",
    "input_video": "video",
    "input_videos": "video",
    "audio": "audio",
    "audios": "audio",
    "audio_url": "audio",
    "audio_urls": "audio",
    "reference_audio": "audio",
    "reference_audios": "audio",
    "input_audio": "audio",
    "input_audios": "audio",
}


def request_operation(method: str, path: str) -> str:
    return _REQUEST_OPERATIONS.get(
        (str(method or "").upper(), str(path or "")),
        "",
    )


def _snake_key(value: Any) -> str:
    text = str(value or "").strip().rstrip("[]")
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.replace("-", "_").lower()


def _put_form_value(target: dict[str, Any], key: str, value: Any) -> None:
    normalized = _snake_key(key)
    if not normalized:
        return
    if normalized not in target:
        target[normalized] = value
        return
    existing = target[normalized]
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(value)
    target[normalized] = existing


def _parse_multipart(raw_body: bytes, content_type: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    uploaded_files: list[dict[str, str]] = []
    header = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
            "utf-8"
        )
    )
    message = BytesParser(policy=policy.default).parsebytes(header + raw_body)
    if not message.is_multipart():
        return data
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        filename = part.get_filename()
        if filename:
            normalized_name = _snake_key(field_name)
            uploaded_files.append(
                {
                    "field": normalized_name,
                    "name": str(filename)[:180],
                    "content_type": str(part.get_content_type() or "")[:100],
                }
            )
            continue
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        _put_form_value(data, str(field_name), text)
    if uploaded_files:
        data["_uploaded_files"] = uploaded_files
    return data


def _parse_request_body(raw_body: bytes, content_type: str) -> dict[str, Any]:
    if not raw_body:
        return {}
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    try:
        if media_type == "application/x-www-form-urlencoded":
            parsed = parse_qs(
                raw_body.decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )
            return {
                _snake_key(key): values[0] if len(values) == 1 else values
                for key, values in parsed.items()
            }
        if media_type == "multipart/form-data":
            return _parse_multipart(raw_body, content_type)
        data = json.loads(raw_body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def _first_value(sources: list[dict[str, Any]], *keys: str) -> Any:
    normalized_keys = [_snake_key(key) for key in keys]
    for source in sources:
        if not isinstance(source, dict):
            continue
        normalized_source = {_snake_key(key): value for key, value in source.items()}
        for key in normalized_keys:
            value = normalized_source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _prompt_preview(data: dict[str, Any]) -> str:
    direct = _text(data.get("prompt"))
    if direct:
        return direct.replace("\r", " ").replace("\n", " ")[:180]

    parts: list[str] = []

    def walk(value: Any) -> None:
        if len(" ".join(parts)) >= 180:
            return
        if isinstance(value, str):
            text = value.strip()
            if text and not text.lower().startswith("data:"):
                parts.append(text)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        item_type = _snake_key(value.get("type"))
        if item_type in {"text", "input_text"}:
            walk(value.get("text"))
        elif "content" in value:
            walk(value.get("content"))

    walk(data.get("messages"))
    walk(data.get("input"))
    if not parts:
        entity_name = _text(data.get("name") or data.get("displayName"))
        if entity_name:
            description = _text(data.get("description"))
            preview = f"entity: {entity_name}"
            if description:
                preview = f"{preview} - {description}"
            return preview[:180]
    return " ".join(parts).replace("\r", " ").replace("\n", " ")[:180]


def _asset_label(kind: str, url: str = "", name: str = "") -> str:
    if name:
        return name[:120]
    if url:
        try:
            parsed = urlparse(url)
            filename = PurePosixPath(parsed.path).name
            if filename:
                return filename[:120]
            if parsed.netloc:
                return parsed.netloc[:120]
        except Exception:
            pass
    return {"image": "参考图", "video": "参考视频", "audio": "参考音频"}.get(
        kind, "参考素材"
    )


def _append_asset(
    assets: list[dict[str, str]],
    *,
    kind: str,
    source: str,
    url: str = "",
    name: str = "",
) -> None:
    if len(assets) >= MAX_REFERENCE_ASSETS:
        return
    item = {
        "kind": kind,
        "source": source,
        "label": _asset_label(kind, url=url, name=name),
    }
    if url:
        item["url"] = url[:2048]
    if name:
        item["name"] = name[:180]
    signature = (
        item.get("kind"),
        item.get("source"),
        item.get("url"),
        item.get("name"),
    )
    for existing in assets:
        existing_signature = (
            existing.get("kind"),
            existing.get("source"),
            existing.get("url"),
            existing.get("name"),
        )
        if signature == existing_signature:
            return
    assets.append(item)


def _collect_asset_value(value: Any, kind: str, assets: list[dict[str, str]]) -> None:
    if value is None or len(assets) >= MAX_REFERENCE_ASSETS:
        return
    if isinstance(value, list):
        for item in value:
            _collect_asset_value(item, kind, assets)
        return
    if isinstance(value, dict):
        nested = value.get(f"{kind}_url") or value.get("url") or value.get("src")
        if isinstance(nested, dict):
            nested = nested.get("url") or nested.get(f"{kind}_url")
        if nested is not None:
            _collect_asset_value(nested, kind, assets)
            return
        if value.get("b64_json") is not None or value.get("data") is not None:
            _append_asset(assets, kind=kind, source="data")
        return
    text = _text(value)
    if not text:
        return
    if text.lower().startswith(("http://", "https://")):
        _append_asset(assets, kind=kind, source="url", url=text)
    elif text.lower().startswith("data:") or len(text) > 512:
        _append_asset(assets, kind=kind, source="data")
    else:
        _append_asset(assets, kind=kind, source="value", name=text)


def _reference_assets(data: dict[str, Any]) -> list[dict[str, str]]:
    assets: list[dict[str, str]] = []

    def walk(value: Any) -> None:
        if len(assets) >= MAX_REFERENCE_ASSETS:
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for raw_key, item in value.items():
            key = _snake_key(raw_key)
            kind = _ASSET_KEYS.get(key)
            if kind:
                _collect_asset_value(item, kind, assets)
            elif key in {"messages", "input", "content", "metadata"}:
                walk(item)

    walk(data)
    for uploaded in data.get("_uploaded_files") or []:
        if not isinstance(uploaded, dict):
            continue
        field = _snake_key(uploaded.get("field"))
        kind = _ASSET_KEYS.get(field)
        if kind:
            _append_asset(
                assets,
                kind=kind,
                source="upload",
                name=_text(uploaded.get("name")) or "uploaded-file",
            )
    return assets


def _model_hints(model: str) -> dict[str, Any]:
    normalized = str(model or "").strip().lower()
    hints: dict[str, Any] = {}
    if not normalized:
        return hints
    ratio_match = re.search(
        r"(?:^|-)(1x1|16x9|9x16|4x3|3x4|3x2|2x3)(?:-|$)",
        normalized,
    )
    if ratio_match:
        hints["ratio"] = ratio_match.group(1).replace("x", ":")
    resolution_match = re.search(
        r"(?:^|-)(1k|2k|4k|720p|1080p)(?:-|$)", normalized
    )
    if resolution_match:
        hints["resolution"] = resolution_match.group(1).upper().replace("P", "p")
    duration_match = re.search(r"(?:^|-)(\d{1,2})s(?:-|$)", normalized)
    if duration_match:
        hints["duration"] = int(duration_match.group(1))
    if any(token in normalized for token in ("sora", "veo", "kling", "seedance")):
        hints["media_type"] = "video"
    else:
        hints["media_type"] = "image"
    return hints


def extract_logging_fields(raw_body: bytes, content_type: str = "") -> dict[str, Any]:
    data = _parse_request_body(raw_body, content_type)
    if not data:
        return {"model": None, "prompt_preview": None, "request_params": {}}

    model = _text(data.get("model")) or None
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    image_tool: dict[str, Any] = {}
    for tool in data.get("tools") or []:
        if (
            isinstance(tool, dict)
            and _snake_key(tool.get("type")) == "image_generation"
        ):
            image_tool = tool
            break
    sources = [data, metadata, image_tool]
    params = _model_hints(model or "")

    size = _first_value(sources, "size", "dimensions", "image_size")
    ratio = _first_value(sources, "ratio", "aspect_ratio", "aspectRatio")
    if (
        not ratio
        and isinstance(size, str)
        and re.fullmatch(r"\d{1,2}:\d{1,2}", size.strip())
    ):
        ratio, size = size, None
    resolution = _first_value(
        sources,
        "resolution",
        "output_resolution",
        "outputResolution",
    )
    duration = _first_value(sources, "duration", "seconds")
    quality = _first_value(sources, "quality")
    count = _first_value(sources, "n", "count")
    mode = _first_value(
        sources,
        "mode",
        "generation_type",
        "generationType",
    )
    reference_mode = _first_value(sources, "reference_mode", "referenceMode")
    generate_audio = _first_value(sources, "generate_audio", "generateAudio")
    response_format = _first_value(sources, "response_format", "output_format")

    for key, value in (
        ("size", size),
        ("ratio", ratio),
        ("resolution", resolution),
        ("duration", duration),
        ("quality", quality),
        ("n", count),
        ("mode", mode),
        ("reference_mode", reference_mode),
        ("generate_audio", generate_audio),
        ("response_format", response_format),
    ):
        if value not in (None, "", [], {}):
            params[key] = value

    assets = _reference_assets(data)
    if assets:
        params["reference_assets"] = assets
        params["reference_count"] = len(assets)

    entity_name = _text(data.get("name") or data.get("displayName"))
    if entity_name:
        entity_type = _text(data.get("type") or data.get("entityType") or "object")
        model = f"entity:{entity_type or 'object'}"

    return {
        "model": model,
        "prompt_preview": _prompt_preview(data) or None,
        "request_params": params,
    }


def set_request_log_params(request: Any, **updates: Any) -> dict[str, Any]:
    current = getattr(getattr(request, "state", None), "log_request_params", None)
    merged = dict(current) if isinstance(current, dict) else {}
    for key, value in updates.items():
        if value not in (None, "", [], {}):
            merged[_snake_key(key)] = value
    request.state.log_request_params = merged
    return merged
