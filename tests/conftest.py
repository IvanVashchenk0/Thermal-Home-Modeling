"""All default tests use artificial local assets; no EnergyPlus installation."""

import sys

import pytest

from thermal_home_modeling.config.experiment_config import phoenix_example
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.smoke.fixtures import create_settings, write_synthetic_sql


@pytest.fixture
def simulation():
    return phoenix_example()


@pytest.fixture
def settings(tmp_path):
    return create_settings(tmp_path)


@pytest.fixture
def fake_runner():
    return EnergyPlusRunner((sys.executable, "-m", "thermal_home_modeling.smoke.fake_energyplus"), timeout_seconds=30)


@pytest.fixture
def sql_directory(tmp_path):
    write_synthetic_sql(tmp_path / "eplusout.sql")
    return tmp_path
