import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from core.request_logging import (
    extract_logging_fields,
    request_operation,
    set_request_log_params,
)
from core.stores import RequestLogRecord, RequestLogStore


class RequestLoggingTest(unittest.TestCase):
    def test_all_generation_create_routes_are_logged(self):
        expected = {
            "/v1/chat/completions": "chat.completions",
            "/v1/images/generations": "images.generations",
            "/v1/images/edits": "images.edits",
            "/v1/responses": "responses.create",
            "/api/v1/generate": "api.generate",
            "/api/v3/contents/generations/tasks": "contents.generations.create",
        }
        for path, operation in expected.items():
            with self.subTest(path=path):
                self.assertEqual(request_operation("POST", path), operation)

        self.assertEqual(request_operation("GET", "/v1/responses/resp_1"), "")
        self.assertEqual(
            request_operation("GET", "/api/v3/contents/generations/tasks/task_1"),
            "",
        )

    def test_json_image_edit_fields_and_reference_url_are_normalized(self):
        payload = {
            "model": "gpt-image-2-4k",
            "prompt": "change to portrait",
            "image": ["https://static.example.com/source.jpg?signature=test"],
            "size": "2048x1152",
            "ratio": "9:16",
            "resolution": "2k",
            "quality": "high",
            "n": 1,
        }
        result = extract_logging_fields(
            json.dumps(payload).encode("utf-8"), "application/json"
        )

        self.assertEqual(result["model"], "gpt-image-2-4k")
        self.assertEqual(result["prompt_preview"], "change to portrait")
        params = result["request_params"]
        self.assertEqual(params["size"], "2048x1152")
        self.assertEqual(params["ratio"], "9:16")
        self.assertEqual(params["resolution"], "2k")
        self.assertEqual(params["quality"], "high")
        self.assertEqual(params["n"], 1)
        self.assertEqual(params["reference_count"], 1)
        self.assertEqual(params["reference_assets"][0]["kind"], "image")
        self.assertEqual(
            params["reference_assets"][0]["url"], payload["image"][0]
        )

    def test_chat_model_hints_and_embedded_reference_are_safe(self):
        embedded = "data:image/png;base64," + ("A" * 1000)
        payload = {
            "model": "firefly-sora2-8s-9x16",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "animate the character"},
                        {"type": "image_url", "image_url": {"url": embedded}},
                    ],
                }
            ],
        }
        result = extract_logging_fields(
            json.dumps(payload).encode("utf-8"), "application/json"
        )
        params = result["request_params"]

        self.assertEqual(result["prompt_preview"], "animate the character")
        self.assertEqual(params["media_type"], "video")
        self.assertEqual(params["duration"], 8)
        self.assertEqual(params["ratio"], "9:16")
        self.assertEqual(params["reference_assets"][0]["source"], "data")
        self.assertNotIn("url", params["reference_assets"][0])

    def test_seedance_parameters_and_mixed_references_are_collected(self):
        payload = {
            "model": "doubao-seedance-2-0-fast-260128",
            "prompt": "combine the references",
            "duration": 8,
            "ratio": "16:9",
            "resolution": "720p",
            "generationType": 3,
            "image_urls": ["https://example.com/image.png"],
            "video_urls": ["https://example.com/video.mp4"],
            "audio_urls": ["https://example.com/voice.mp3"],
        }
        result = extract_logging_fields(
            json.dumps(payload).encode("utf-8"), "application/json"
        )
        params = result["request_params"]

        self.assertEqual(params["duration"], 8)
        self.assertEqual(params["ratio"], "16:9")
        self.assertEqual(params["resolution"], "720p")
        self.assertEqual(params["mode"], 3)
        self.assertEqual(params["reference_count"], 3)
        self.assertEqual(
            [item["kind"] for item in params["reference_assets"]],
            ["image", "video", "audio"],
        )

    def test_multipart_url_and_uploaded_file_are_both_visible(self):
        request = httpx.Request(
            "POST",
            "https://example.com/v1/images/edits",
            data={
                "model": "gpt-image-2",
                "prompt": "edit",
                "image": "https://example.com/source.jpg",
                "ratio": "1:1",
            },
            files={"image[]": ("reference.png", b"png-bytes", "image/png")},
        )
        result = extract_logging_fields(
            request.read(), request.headers["Content-Type"]
        )
        assets = result["request_params"]["reference_assets"]

        self.assertEqual(result["model"], "gpt-image-2")
        self.assertEqual(result["request_params"]["ratio"], "1:1")
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0]["source"], "url")
        self.assertEqual(assets[1]["source"], "upload")
        self.assertEqual(assets[1]["name"], "reference.png")

    def test_resolved_params_merge_and_persist_in_store(self):
        request = SimpleNamespace(
            state=SimpleNamespace(log_request_params={"size": "1024x1024"})
        )
        merged = set_request_log_params(
            request,
            ratio="16:9",
            resolution="2K",
            duration=None,
        )
        self.assertEqual(
            merged,
            {"size": "1024x1024", "ratio": "16:9", "resolution": "2K"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = RequestLogStore(Path(tmp) / "logs.jsonl")
            store.add(
                RequestLogRecord(
                    id="log-1",
                    ts=1,
                    method="POST",
                    path="/v1/images/generations",
                    status_code=200,
                    duration_sec=1,
                    operation="images.generations",
                    request_params=merged,
                )
            )
            rows, total = store.list()
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["request_params"], merged)

    def test_stats_use_request_media_type_for_async_generation_creates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RequestLogStore(Path(tmp) / "logs.jsonl")
            for index, media_type in enumerate(("image", "video"), start=1):
                store.add(
                    RequestLogRecord(
                        id=f"log-{index}",
                        ts=10 + index,
                        method="POST",
                        path=(
                            "/v1/responses"
                            if media_type == "image"
                            else "/api/v3/contents/generations/tasks"
                        ),
                        status_code=200,
                        duration_sec=0,
                        operation=(
                            "responses.create"
                            if media_type == "image"
                            else "contents.generations.create"
                        ),
                        request_params={"media_type": media_type},
                    )
                )
            stats = store.stats()

        self.assertEqual(stats["generated_images"], 1)
        self.assertEqual(stats["generated_videos"], 1)


if __name__ == "__main__":
    unittest.main()
