import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from api.routes.generation import build_generation_router


class _FakeClient:
    gpt_image_quality = "low"
    generate_timeout = 10

    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str]] = []
        self.generate_kwargs: dict = {}

    def upload_image(self, token: str, image_bytes: bytes, image_mime: str) -> str:
        self.uploads.append((image_bytes, image_mime))
        return f"asset-{len(self.uploads)}"

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        kwargs["progress_cb"](
            {"task_status": "IN_PROGRESS", "task_progress": 50, "upstream_job_id": "job-1"}
        )
        return b"fake-png", {"progress": 100}


def _build_test_app(root: Path, client: _FakeClient, loaded_messages: list) -> FastAPI:
    app = FastAPI()

    def load_input_images(messages):
        loaded_messages.extend(messages)
        return [(b"remote-image", "image/jpeg")]

    app.include_router(
        build_generation_router(
            store=SimpleNamespace(),
            token_manager=SimpleNamespace(),
            client=client,
            generated_dir=root,
            model_catalog={},
            video_model_catalog={},
            supported_ratios={"1:1", "9:16"},
            resolve_model=lambda model_id: {
                "upstream_model_id": "gpt-image",
                "upstream_model_version": "gpt-image-2",
            },
            resolve_ratio_and_resolution=lambda data, model_id: (
                str(data.get("ratio") or data.get("aspect_ratio") or "1:1"),
                "2K",
                str(model_id or "gpt-image-2"),
            ),
            require_service_api_key=lambda request: None,
            set_request_task_progress=lambda *args, **kwargs: None,
            run_with_token_retries=lambda **kwargs: kwargs["run_once"]("token"),
            set_request_error_detail=lambda *args, **kwargs: "error-id",
            set_request_preview=lambda *args, **kwargs: None,
            public_image_url=lambda request, image_id: f"https://example.com/{image_id}.png",
            public_generated_url=lambda request, filename: f"https://example.com/{filename}",
            resolve_video_options=lambda data: (False, "", ""),
            load_input_images=load_input_images,
            prepare_video_source_image=lambda data, ratio, resolution: (data, "image/jpeg"),
            video_ext_from_meta=lambda meta: "mp4",
            extract_prompt_from_messages=lambda messages: "",
            sse_chat_stream=lambda payload: iter(()),
            on_generated_file_written=lambda path, old_size, new_size: None,
            quota_error_cls=RuntimeError,
            auth_error_cls=PermissionError,
            upstream_temp_error_cls=ConnectionError,
            logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
        )
    )
    return app


class ImageEditsEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_json_url_edit_uploads_source_and_generates(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeClient()
            loaded_messages: list = []
            app = _build_test_app(Path(tmp), fake_client, loaded_messages)
            transport = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/images/edits",
                    json={
                        "model": "gpt-image-2",
                        "prompt": "change to portrait",
                        "image": ["https://example.com/source.jpg"],
                        "n": 1,
                        "ratio": "9:16",
                        "response_format": "url",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model"], "gpt-image-2")
            self.assertEqual(len(response.json()["data"]), 1)
            self.assertEqual(
                loaded_messages[0]["content"][0]["image_url"]["url"],
                "https://example.com/source.jpg",
            )
            self.assertEqual(fake_client.uploads, [(b"remote-image", "image/jpeg")])
            self.assertEqual(fake_client.generate_kwargs["source_image_ids"], ["asset-1"])
            self.assertEqual(fake_client.generate_kwargs["aspect_ratio"], "9:16")

    async def test_multipart_file_edit_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeClient()
            app = _build_test_app(Path(tmp), fake_client, [])
            transport = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/images/edits",
                    data={"model": "gpt-image-2", "prompt": "edit", "ratio": "1:1"},
                    files={"image[]": ("source.png", b"png-bytes", "image/png")},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(fake_client.uploads, [(b"png-bytes", "image/png")])
            self.assertEqual(fake_client.generate_kwargs["source_image_ids"], ["asset-1"])

    async def test_form_url_edit_matches_litellm_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_client = _FakeClient()
            loaded_messages: list = []
            app = _build_test_app(Path(tmp), fake_client, loaded_messages)
            transport = httpx.ASGITransport(app=app)

            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/images/edits",
                    data={
                        "model": "gpt-image-2",
                        "prompt": "edit",
                        "image": "https://example.com/source.jpg",
                        "ratio": "9:16",
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                loaded_messages[0]["content"][0]["image_url"]["url"],
                "https://example.com/source.jpg",
            )
            self.assertEqual(fake_client.generate_kwargs["aspect_ratio"], "9:16")

    async def test_image_edit_requires_one_image_and_n_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _build_test_app(Path(tmp), _FakeClient(), [])
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                missing = await client.post(
                    "/v1/images/edits",
                    json={"model": "gpt-image-2", "prompt": "edit"},
                )
                too_many = await client.post(
                    "/v1/images/edits",
                    json={
                        "model": "gpt-image-2",
                        "prompt": "edit",
                        "image": ["https://example.com/source.jpg"],
                        "n": 2,
                    },
                )

            self.assertEqual(missing.status_code, 422)
            self.assertEqual(too_many.status_code, 400)
            self.assertIn("supports n=1", too_many.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
