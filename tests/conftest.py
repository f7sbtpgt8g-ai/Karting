import os
import runpy

import pytest

from telemetry.parser import load_sessions

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_session.tsv")
GENERATOR_PATH = os.path.join(os.path.dirname(__file__), "generate_fixture.py")


@pytest.fixture(scope="session")
def sessions():
    if not os.path.exists(FIXTURE_PATH):
        # Not checked into git (it's a multi-MB generated file) -- build it
        # once here instead. See tests/generate_fixture.py for why it exists:
        # the real sample export the original spec referenced wasn't
        # actually available in this environment.
        runpy.run_path(GENERATOR_PATH, run_name="__main__")
    return load_sessions(FIXTURE_PATH)


@pytest.fixture(scope="session")
def session1(sessions):
    return sessions[0]


@pytest.fixture(scope="session")
def session2(sessions):
    return sessions[1]
