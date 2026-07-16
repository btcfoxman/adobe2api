import os
import unittest
from unittest.mock import patch

from core.refresh_mgr import RefreshManager


class RefreshProxyTests(unittest.TestCase):
    def setUp(self):
        self.manager = RefreshManager.__new__(RefreshManager)

    def test_adobe_proxy_environment_overrides_disabled_config(self):
        with patch.dict(os.environ, {"ADOBE_PROXY": " http://host.docker.internal:10809 "}, clear=False):
            with patch("core.refresh_mgr.config_manager.get", return_value=False):
                self.assertEqual(
                    self.manager._requests_proxies(),
                    {
                        "http": "http://host.docker.internal:10809",
                        "https": "http://host.docker.internal:10809",
                    },
                )

    def test_empty_adobe_proxy_environment_disables_config_proxy(self):
        with patch.dict(os.environ, {"ADOBE_PROXY": ""}, clear=False):
            with patch("core.refresh_mgr.config_manager.get", side_effect=["http://config-proxy:8080", True]):
                self.assertIsNone(self.manager._requests_proxies())

    def test_config_proxy_is_used_when_environment_is_absent(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADOBE_PROXY", None)
            with patch("core.refresh_mgr.config_manager.get", side_effect=["http://config-proxy:8080", True]):
                self.assertEqual(
                    self.manager._requests_proxies(),
                    {
                        "http": "http://config-proxy:8080",
                        "https": "http://config-proxy:8080",
                    },
                )


if __name__ == "__main__":
    unittest.main()
