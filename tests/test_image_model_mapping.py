import unittest

from fastapi import HTTPException

from core.models import (
    default_external_image_model_mappings,
    resolve_ratio_and_resolution,
    validate_external_image_model_mappings,
)


class ExternalImageModelMappingTest(unittest.TestCase):
    def test_maps_standard_model_and_preserves_ratio_resolution(self):
        ratio, resolution, model = resolve_ratio_and_resolution(
            {
                "size": "2048x1152",
                "ratio": "16:9",
                "resolution": "2k",
            },
            "gpt-image-2",
            default_external_image_model_mappings(),
        )
        self.assertEqual(ratio, "16:9")
        self.assertEqual(resolution, "2K")
        self.assertEqual(model, "firefly-gpt-image-2k-16x9")

    def test_explicit_resolution_overrides_model_suffix(self):
        ratio, resolution, model = resolve_ratio_and_resolution(
            {"ratio": "9:16", "resolution": "2k"},
            "nano-banana-2-4k",
            default_external_image_model_mappings(),
        )
        self.assertEqual((ratio, resolution), ("9:16", "2K"))
        self.assertEqual(model, "firefly-nano-banana2-2k-9x16")

    def test_model_suffix_is_resolution_hint_without_explicit_value(self):
        ratio, resolution, model = resolve_ratio_and_resolution(
            {"aspect_ratio": "1:1"},
            "nano-banana-pro-4k",
            default_external_image_model_mappings(),
        )
        self.assertEqual((ratio, resolution), ("1:1", "4K"))
        self.assertEqual(model, "firefly-nano-banana-pro-4k-1x1")

    def test_rejects_unsupported_mapping_combination(self):
        with self.assertRaises(HTTPException) as raised:
            resolve_ratio_and_resolution(
                {"ratio": "1:8", "resolution": "2k"},
                "nano-banana-pro",
                default_external_image_model_mappings(),
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_rejects_unmapped_standard_model_when_config_is_explicit(self):
        with self.assertRaises(HTTPException) as raised:
            resolve_ratio_and_resolution(
                {"ratio": "1:1", "resolution": "2k"},
                "unknown-image-model",
                default_external_image_model_mappings(),
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_validates_mapping_targets(self):
        invalid = default_external_image_model_mappings()
        invalid["gpt-image-2"]["template"] = "missing-{resolution}-{ratio}"
        with self.assertRaises(ValueError):
            validate_external_image_model_mappings(invalid)


if __name__ == "__main__":
    unittest.main()
