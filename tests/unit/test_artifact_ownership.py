"""Campaign fixture temporary directories have an explicit owner on every exit."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import recorded_campaign


class CampaignArtifactOwnershipTests(unittest.TestCase):
    def test_execute_cleans_its_directory_before_propagating_failure(self):
        holder = tempfile.TemporaryDirectory()
        root = Path(holder.name)
        self.addCleanup(holder.cleanup)
        with (
            mock.patch.object(
                recorded_campaign.tempfile, "TemporaryDirectory", return_value=holder
            ),
            mock.patch.object(
                recorded_campaign, "RecordedCampaign", side_effect=ValueError("stop")
            ),
            mock.patch.object(holder, "cleanup", wraps=holder.cleanup) as cleanup,
        ):
            with self.assertRaisesRegex(ValueError, "stop"):
                recorded_campaign.execute(recorded_campaign.protocol(1))
            cleanup.assert_called_once_with()
        self.assertFalse(root.exists())

    def test_execute_does_not_clean_caller_owned_directory_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                recorded_campaign, "RecordedCampaign", side_effect=ValueError("stop")
            ):
                with self.assertRaisesRegex(ValueError, "stop"):
                    recorded_campaign.execute(recorded_campaign.protocol(1), root=root)
            self.assertTrue(root.exists())
