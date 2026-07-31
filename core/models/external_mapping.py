from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from .catalog import MODEL_CATALOG, SUPPORTED_RATIOS


DEFAULT_EXTERNAL_IMAGE_MODEL_MAPPINGS: dict[str, dict[str, Any]] = {
    "gpt-image-2": {
        "template": "firefly-gpt-image-{resolution}-{ratio}",
        "default_resolution": "2k",
        "default_ratio": "1:1",
    },
    "nano-banana-2": {
        "template": "firefly-nano-banana2-{resolution}-{ratio}",
        "default_resolution": "2k",
        "default_ratio": "1:1",
    },
    "nano-banana-pro": {
        "template": "firefly-nano-banana-pro-{resolution}-{ratio}",
        "default_resolution": "2k",
        "default_ratio": "1:1",
    },
}

_TIER_RE = re.compile(r"(?<![a-z0-9])(1k|2k|4k)(?![a-z0-9])", re.IGNORECASE)
_MODEL_TIER_RE = re.compile(r"^(.*)-(1k|2k|4k)$", re.IGNORECASE)
_RATIO_RE = re.compile(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)")
_PIXEL_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")


def default_external_image_model_mappings() -> dict[str, dict[str, Any]]:
    return deepcopy(DEFAULT_EXTERNAL_IMAGE_MODEL_MAPPINGS)


def validate_external_image_model_mappings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("image_model_mappings must be a non-empty object")
    normalized: dict[str, Any] = {}
    for raw_name, raw_entry in value.items():
        name = str(raw_name or "").strip()
        if not name:
            raise ValueError("image_model_mappings contains an empty model name")
        if isinstance(raw_entry, str):
            target = raw_entry.strip()
            if not target:
                raise ValueError(f"image_model_mappings.{name} cannot be empty")
            if target not in MODEL_CATALOG:
                raise ValueError(
                    f"image_model_mappings.{name} maps to unknown model: {target}"
                )
            normalized[name] = target
            continue
        if not isinstance(raw_entry, dict):
            raise ValueError(f"image_model_mappings.{name} must be a string or object")
        entry = dict(raw_entry)
        template = str(entry.get("template") or entry.get("model") or "").strip()
        if not template:
            raise ValueError(f"image_model_mappings.{name}.template is required")
        default_resolution = str(entry.get("default_resolution") or "2k").strip().lower()
        if default_resolution not in {"1k", "2k", "4k"}:
            raise ValueError(
                f"image_model_mappings.{name}.default_resolution must be 1k, 2k, or 4k"
            )
        default_ratio = _normalize_ratio(entry.get("default_ratio"))
        if not default_ratio:
            raise ValueError(f"image_model_mappings.{name}.default_ratio is invalid")
        overrides = entry.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError(f"image_model_mappings.{name}.overrides must be an object")
        entry["template"] = template
        entry["default_resolution"] = default_resolution
        entry["default_ratio"] = default_ratio
        entry["overrides"] = {
            str(key).strip(): str(target).strip()
            for key, target in overrides.items()
            if str(key).strip() and str(target).strip()
        }
        for target in entry["overrides"].values():
            if target not in MODEL_CATALOG:
                raise ValueError(
                    f"image_model_mappings.{name}.overrides contains unknown model: {target}"
                )
        try:
            default_target = template.format(
                model=name,
                resolution=default_resolution,
                ratio=default_ratio.replace(":", "x"),
                ratio_colon=default_ratio,
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"image_model_mappings.{name}.template is invalid: {exc}"
            ) from exc
        if default_target not in MODEL_CATALOG:
            raise ValueError(
                f"image_model_mappings.{name} default maps to unknown model: {default_target}"
            )
        entry.pop("model", None)
        normalized[name] = entry
    return normalized


def _tier_from_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    match = _TIER_RE.search(text)
    if match:
        return match.group(1).lower()
    pixels = _pixel_dimensions(text)
    if not pixels:
        return ""
    longest = max(pixels)
    if longest <= 1024:
        return "1k"
    if longest <= 2048:
        return "2k"
    return "4k"


def _pixel_dimensions(value: Any) -> tuple[int, int] | None:
    match = _PIXEL_RE.match(str(value or "").strip())
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


def _ratio_value(ratio: str) -> float | None:
    match = _RATIO_RE.search(str(ratio or ""))
    if not match:
        return None
    width, height = float(match.group(1)), float(match.group(2))
    return width / height if width > 0 and height > 0 else None


def _normalize_ratio(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    pixels = _pixel_dimensions(text)
    if pixels:
        target = pixels[0] / pixels[1]
    else:
        parsed = _ratio_value(text)
        if parsed is None:
            return ""
        target = parsed
    return min(
        SUPPORTED_RATIOS,
        key=lambda candidate: abs((_ratio_value(candidate) or 1.0) - target),
    )


def _mapping_entry(
    model_id: str, mappings: dict[str, Any]
) -> tuple[str, Any, str] | None:
    requested = str(model_id or "").strip()
    if requested in mappings:
        suffix_match = _MODEL_TIER_RE.match(requested)
        return requested, mappings[requested], suffix_match.group(2).lower() if suffix_match else ""
    match = _MODEL_TIER_RE.match(requested)
    if match and match.group(1) in mappings:
        base = match.group(1)
        return base, mappings[base], match.group(2).lower()
    return None


def resolve_external_image_model(
    data: dict[str, Any],
    model_id: str | None,
    mappings: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    requested = str(model_id or "").strip()
    if not requested or requested in MODEL_CATALOG:
        return None
    effective_mappings = mappings or DEFAULT_EXTERNAL_IMAGE_MODEL_MAPPINGS
    found = _mapping_entry(requested, effective_mappings)
    if found is None:
        return None
    mapping_name, raw_entry, model_tier = found
    if isinstance(raw_entry, str):
        entry: dict[str, Any] = {
            "template": raw_entry,
            "default_resolution": "2k",
            "default_ratio": "1:1",
            "overrides": {},
        }
    else:
        entry = dict(raw_entry or {})

    resolution = (
        _tier_from_value(data.get("resolution"))
        or _tier_from_value(data.get("size"))
        or model_tier
        or str(entry.get("default_resolution") or "2k").lower()
    )
    ratio = (
        _normalize_ratio(data.get("ratio"))
        or _normalize_ratio(data.get("aspect_ratio"))
        or _normalize_ratio(data.get("size"))
        or _normalize_ratio(data.get("resolution"))
        or _normalize_ratio(entry.get("default_ratio"))
        or "1:1"
    )
    ratio_suffix = ratio.replace(":", "x")
    overrides = entry.get("overrides") if isinstance(entry.get("overrides"), dict) else {}
    target = str(
        overrides.get(f"{resolution}:{ratio}")
        or overrides.get(f"{resolution}/{ratio}")
        or entry.get("template")
        or entry.get("model")
        or ""
    ).strip()
    try:
        internal_model_id = target.format(
            model=mapping_name,
            resolution=resolution,
            ratio=ratio_suffix,
            ratio_colon=ratio,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Invalid image model mapping: {exc}")
    if internal_model_id not in MODEL_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported image mapping combination: model={requested}, "
                f"resolution={resolution}, ratio={ratio}"
            ),
        )
    conf = MODEL_CATALOG[internal_model_id]
    return (
        str(conf.get("aspect_ratio") or ratio),
        str(conf.get("output_resolution") or resolution.upper()),
        internal_model_id,
    )
