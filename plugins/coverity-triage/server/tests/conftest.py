"""Keep the user's ~/.copilot/agents out of the tests."""

import shutil

import pytest

from coverity_triage import onboarding


@pytest.fixture(autouse=True)
def user_agents(tmp_path_factory, monkeypatch):
    """A user-level agents folder that already holds the current worker agent."""
    folder = tmp_path_factory.mktemp("copilot-agents")
    shutil.copyfile(onboarding.WORKER_AGENT, folder / onboarding.WORKER_AGENT.name)
    monkeypatch.setattr(onboarding, "user_agents_dir", lambda: folder)
    return folder
