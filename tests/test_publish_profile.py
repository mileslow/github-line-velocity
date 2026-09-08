import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_profile
from scripts.generate_profile import ApiError


class PublishRecoveryTests(unittest.TestCase):
    def test_conflict_refetches_sha_before_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / "profile.svg"
            svg.write_text("<svg/>")
            with patch("sys.argv", ["publish_profile", "--svg", str(svg), "--repo", "test/profile", "--path", "profile.svg"]), \
                 patch.dict("os.environ", {"GH_TOKEN": "test"}), \
                 patch.object(publish_profile, "request", side_effect=[
                     {"sha": "old", "content": ""}, ApiError("conflict", status=409),
                     {"sha": "new", "content": ""}, {"commit": {"html_url": "https://example.com/commit"}},
                 ]) as request:
                self.assertEqual(publish_profile.main(), 0)
                self.assertEqual(request.call_args_list[1].args[3]["sha"], "old")
                self.assertEqual(request.call_args_list[3].args[3]["sha"], "new")

    def test_retry_after_lost_response_recognizes_already_published_content(self):
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / "profile.svg"
            svg.write_text("<svg/>")
            with patch("sys.argv", ["publish_profile", "--svg", str(svg), "--repo", "test/profile", "--path", "profile.svg"]), \
                 patch.dict("os.environ", {"GH_TOKEN": "test"}), \
                 patch.object(publish_profile, "request", return_value={
                     "sha": "new", "content": base64.b64encode(svg.read_bytes()).decode(),
                 }) as request:
                self.assertEqual(publish_profile.main(), 0)
                self.assertEqual(request.call_count, 1)
