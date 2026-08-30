from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from handsfree_pc.cli import command_computer_doctor


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("HANDSFREEPC_RUN_LIVE") != "1",
    reason="set HANDSFREEPC_RUN_LIVE=1 for the visible owned-fixture smoke test",
)
def test_owned_windows_uia_fixture_round_trip(tmp_path):
    config = tmp_path / "live-config.yaml"
    config.write_text(
        """
privacy:
  allow_cloud_planner: false
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
execution:
  dry_run: false
""",
        encoding="utf-8",
    )

    assert command_computer_doctor(SimpleNamespace(config=str(config), live=True)) == 0
