import unittest
from types import SimpleNamespace

from api.routes.seedance import (
    SEEDANCE_FRAME_MODE,
    _build_reference_blobs,
    _duration_seconds,
    _normalize_generation_mode,
    _normalize_ratio,
    _normalize_resolution,
    _task_payload,
    _validate_seedance_request,
)
from core.adobe_client import AdobeClient
from core.s3_uploader import S3Uploader


class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = str(data)
        self.headers = {}

    def json(self):
        return self._data


class _FakeS3Response:
    status_code = 200
    text = ""


class SeedanceHelpersTest(unittest.TestCase):
    def test_duration_parses_helper_seconds(self):
        self.assertEqual(_duration_seconds("~15s"), 15)
        self.assertEqual(_duration_seconds("4 seconds"), 4)

    def test_validate_public_params(self):
        self.assertEqual(_normalize_ratio("720x1280"), "9:16")
        self.assertEqual(_normalize_resolution("720"), "720p")
        with self.assertRaises(ValueError):
            _validate_seedance_request(
                duration=16,
                ratio="16:9",
                resolution="720p",
                image_urls=[],
                video_urls=[],
                audio_urls=[],
            )

    def test_reference_blobs_use_unique_mentions(self):
        prompt, refs = _build_reference_blobs(
            "use @图片1 with @视频1 and @音频1",
            [{"id": "image-id"}],
            [{"id": "video-id"}],
            [{"id": "audio-id"}],
        )
        mention_ids = [item["mention"]["id"] for item in refs]
        self.assertEqual(len(mention_ids), len(set(mention_ids)))
        self.assertNotIn("@图片1", prompt)
        self.assertEqual([item["usage"] for item in refs], ["style", "source", "source"])


    def test_frame_mode_uses_ordered_frame_blobs(self):
        prompt, refs = _build_reference_blobs(
            "transition from @Image1 to @Image2",
            [{"id": "first-frame"}, {"id": "last-frame"}],
            [],
            [],
            image_mode=SEEDANCE_FRAME_MODE,
        )
        self.assertEqual(prompt, "transition from @Image1 to @Image2")
        self.assertEqual(
            refs,
            [
                {"id": "first-frame", "usage": "frame", "order": 1},
                {"id": "last-frame", "usage": "frame", "order": 2},
            ],
        )
        self.assertEqual(
            _normalize_generation_mode({"generationType": 2}, []),
            SEEDANCE_FRAME_MODE,
        )

    def test_frame_mode_validates_one_or_two_images(self):
        _validate_seedance_request(
            duration=5,
            ratio="16:9",
            resolution="720p",
            mode=SEEDANCE_FRAME_MODE,
            image_urls=["https://example.test/first.png"],
            video_urls=[],
            audio_urls=[],
        )
        with self.assertRaises(ValueError):
            _validate_seedance_request(
                duration=5,
                ratio="16:9",
                resolution="720p",
                mode=SEEDANCE_FRAME_MODE,
                image_urls=[
                    "https://example.test/1.png",
                    "https://example.test/2.png",
                    "https://example.test/3.png",
                ],
                video_urls=[],
                audio_urls=[],
            )

    def test_query_payload_omits_helper_task_id_and_provider(self):
        task = SimpleNamespace(
            id="task_1",
            status="succeeded",
            progress=100.0,
            video_filename="task_1.mp4",
            video_url="https://cdn.example.com/task_1.mp4",
            error_code="",
            error="",
            upstream_task_id="upstream_1",
            model="doubao-seedance-2-0-fast-260128",
            created_at=1,
            updated_at=2,
            completed_at=3,
        )
        payload = _task_payload(
            task,
            None,
            lambda _request, filename: f"http://local/{filename}",
            include_public_metadata=False,
        )
        self.assertNotIn("helper_task_id", payload)
        self.assertNotIn("provider", payload)
        self.assertEqual(payload["video_url"], "https://cdn.example.com/task_1.mp4")


class SeedanceAdobeClientTest(unittest.TestCase):
    def test_storage_asset_parses_image_and_asset_ids(self):
        client = AdobeClient()
        seen = []

        def fake_post(url, headers, payload):
            seen.append((url, headers, payload))
            if url.endswith("/image"):
                return _FakeResponse({"images": [{"id": "image-asset"}]})
            return _FakeResponse({"assets": [{"id": "media-asset"}]})

        client._post_bytes = fake_post
        self.assertEqual(
            client.upload_storage_asset("token", "image", b"x", "image/png"),
            "image-asset",
        )
        self.assertEqual(
            client.upload_storage_asset("token", "audio", b"x", "audio/mpeg"),
            "media-asset",
        )
        self.assertEqual(
            client.upload_storage_asset("token", "video", b"x", "video/mp4"),
            "media-asset",
        )
        self.assertEqual(len(seen), 3)

    def test_seedance_payload_shape(self):
        client = AdobeClient()
        payload = client.build_seedance_video_payload(
            prompt="hello",
            duration=12,
            aspect_ratio="3:4",
            reference_blobs=[{"id": "image-asset", "usage": "style"}],
        )
        self.assertEqual(payload["modelId"], "seedance")
        self.assertEqual(payload["modelVersion"], "seedance_2.0_fast")
        self.assertEqual(payload["size"], {"width": 720, "height": 960})
        self.assertEqual(payload["duration"], 12)
        self.assertTrue(payload["generateAudio"])
        self.assertEqual(payload["referenceBlobs"][0]["usage"], "style")

    def test_seedance_standard_payload_model_version(self):
        client = AdobeClient()
        payload = client.build_seedance_video_payload(
            prompt="hello",
            model_version="seedance_2.0",
            duration=5,
            aspect_ratio="16:9",
            reference_blobs=[
                {"id": "first-frame", "usage": "frame", "order": 1},
                {"id": "last-frame", "usage": "frame", "order": 2},
            ],
            generate_audio=False,
        )
        self.assertEqual(payload["modelVersion"], "seedance_2.0")
        self.assertFalse(payload["generateAudio"])
        self.assertEqual(payload["referenceBlobs"][1]["order"], 2)


class SeedanceS3UploaderTest(unittest.TestCase):
    def test_s3_upload_returns_public_url(self):
        import tempfile
        import core.s3_uploader as s3_module

        calls = []
        old_put = s3_module.requests.put

        def fake_put(url, data, headers, timeout):
            calls.append((url, data, headers, timeout))
            return _FakeS3Response()

        with tempfile.TemporaryDirectory() as tmp:
            path = __import__("pathlib").Path(tmp) / "video.mp4"
            path.write_bytes(b"video")
            s3_module.requests.put = fake_put
            try:
                url = S3Uploader(
                    {
                        "s3_enabled": True,
                        "s3_endpoint": "https://s3.example.com",
                        "s3_region": "auto",
                        "s3_bucket": "bucket",
                        "s3_access_key": "ak",
                        "s3_secret_key": "sk",
                        "s3_prefix": "adobe2api/generated/",
                        "s3_public_base_url": "https://cdn.example.com/assets",
                        "s3_force_path_style": True,
                    }
                ).upload_file(path, content_type="video/mp4")
            finally:
                s3_module.requests.put = old_put
        self.assertEqual(url, "https://cdn.example.com/assets/adobe2api/generated/video.mp4")
        self.assertEqual(calls[0][0], "https://s3.example.com/bucket/adobe2api/generated/video.mp4")
        self.assertIn("authorization", calls[0][2])


if __name__ == "__main__":
    unittest.main()
