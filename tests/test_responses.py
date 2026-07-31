import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx

from api.routes.responses import (
    build_responses_router,
    image_urls_from_responses_input,
    prompt_from_responses_input,
    response_from_task,
)
from api.schemas import ResponsesCreateRequest
from core.models import (
    DEFAULT_MODEL_ID,
    VIDEO_MODEL_CATALOG,
    resolve_model,
    resolve_ratio_and_resolution,
)
from core.response_tasks import ResponseTaskStore


class ResponsesHelpersTest(unittest.TestCase):
    def test_extracts_prompt_and_input_images(self):
        payload = ResponsesCreateRequest(
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "change the background"},
                        {"type": "input_image", "image_url": "https://example.com/a.png"},
                        {"type": "input_image", "image_url": {"url": "https://example.com/b.png"}},
                    ],
                }
            ]
        )
        self.assertEqual(prompt_from_responses_input(payload), "change the background")
        self.assertEqual(
            image_urls_from_responses_input(payload),
            ["https://example.com/a.png", "https://example.com/b.png"],
        )

    def test_response_shape_matches_imgs2api(self):
        task = SimpleNamespace(
            id="imgtask_test",
            response_id="resp_test",
            prompt="draw",
            model="firefly-nano-banana-pro-2k-16x9",
            aspect_ratio="16:9",
            output_resolution="2K",
            status="succeeded",
            progress=100.0,
            result_urls=["https://example.com/out.png"],
            input_image_count=0,
            context={
                "model": "firefly-nano-banana-pro-2k-16x9",
                "output_type": "image_generation_call",
                "metadata": {"serial": "abc"},
                "parallel_tool_calls": True,
            },
            upstream_job_id="upstream_test",
            error="",
            created_at=1781827200,
            updated_at=1781827210,
            completed_at=1781827210,
        )
        response = response_from_task(task)
        self.assertEqual(response["id"], "resp_test")
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["metadata"]["task_id"], "imgtask_test")
        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(response["output"][0]["url"], "https://example.com/out.png")


class ResponseTaskStoreTest(unittest.TestCase):
    def test_completed_tasks_survive_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "responses.db"
            store = ResponseTaskStore(db_path)
            task = store.create(
                prompt="draw",
                model=DEFAULT_MODEL_ID,
                aspect_ratio="16:9",
                output_resolution="2K",
                input_image_count=0,
                context={"model": DEFAULT_MODEL_ID},
            )
            store.update(
                task.id,
                status="succeeded",
                progress=100.0,
                result_urls=["https://example.com/out.png"],
                completed_at=int(time.time()),
            )

            reopened = ResponseTaskStore(db_path)
            restored = reopened.get(task.response_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, "succeeded")
            self.assertEqual(restored.result_urls, ["https://example.com/out.png"])

    def test_incomplete_tasks_are_failed_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "responses.db"
            store = ResponseTaskStore(db_path)
            task = store.create(
                prompt="draw",
                model=DEFAULT_MODEL_ID,
                aspect_ratio="16:9",
                output_resolution="2K",
                input_image_count=0,
                context={"model": DEFAULT_MODEL_ID},
            )

            restored = ResponseTaskStore(db_path).get(task.id)
            self.assertEqual(restored.status, "failed")
            self.assertIn("service restart", restored.error)


class _FakeError(Exception):
    pass


class _FakeTokenManager:
    def get_available(self, strategy=None):
        return "token"

    def report_exhausted(self, token):
        pass

    def report_invalid(self, token):
        pass


class _FakeClient:
    retry_enabled = False
    retry_max_attempts = 1
    token_rotation_strategy = "round_robin"
    gpt_image_quality = "low"
    generate_timeout = 10

    def upload_image(self, token, image_bytes, image_mime):
        return "asset"

    def generate(self, **kwargs):
        kwargs["progress_cb"](
            {"task_status": "IN_PROGRESS", "task_progress": 50, "upstream_job_id": "job_1"}
        )
        return b"fake-png", {"progress": 100}

    def should_retry_temporary_error(self, exc):
        return False

    def _retry_delay_for_attempt(self, attempt):
        return 0


class ResponsesEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_get_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResponseTaskStore(root / "responses.db")
            app = FastAPI()
            app.include_router(
                build_responses_router(
                    store=store,
                    token_manager=_FakeTokenManager(),
                    client=_FakeClient(),
                    generated_dir=root,
                    video_model_catalog=VIDEO_MODEL_CATALOG,
                    default_model_id=DEFAULT_MODEL_ID,
                    resolve_model=resolve_model,
                    resolve_ratio_and_resolution=resolve_ratio_and_resolution,
                    require_service_api_key=lambda request: None,
                    public_image_url=lambda request, task_id: f"https://example.com/{task_id}.png",
                    load_input_images=lambda messages: [],
                    on_generated_file_written=lambda path, old, new: None,
                    quota_error_cls=_FakeError,
                    auth_error_cls=_FakeError,
                    upstream_temp_error_cls=_FakeError,
                    logger=SimpleNamespace(exception=lambda *args, **kwargs: None),
                )
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    "/v1/responses",
                    json={
                        "model": "gpt-image-2-4k",
                        "input": "draw a fox",
                        "tools": [{"type": "image_generation"}],
                        "ratio": "16:9",
                        "resolution": "2k",
                        "background": True,
                    },
                )
                self.assertEqual(created.status_code, 200)
                response_id = created.json()["id"]
                self.assertTrue(response_id.startswith("resp_"))

                result = None
                for _ in range(100):
                    result = await client.get(f"/v1/responses/{response_id}")
                    if result.json().get("status") == "completed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["status"], "completed")
                self.assertEqual(result.json()["model"], "gpt-image-2-4k")
                self.assertEqual(result.json()["output"][0]["type"], "image_generation_call")
                stored = store.get(response_id)
                self.assertEqual(stored.model, "firefly-gpt-image-2k-16x9")
                self.assertEqual(stored.output_resolution, "2K")


if __name__ == "__main__":
    unittest.main()
