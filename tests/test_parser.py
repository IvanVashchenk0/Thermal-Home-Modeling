from dataclasses import replace
import sqlite3

import pytest

from thermal_home_modeling.config.output_config import OutputConfig, PeakVariable
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.errors import ResultParsingError


def execute(directory, statement):
    with sqlite3.connect(directory / "eplusout.sql") as connection:
        connection.executescript(statement)


def test_annual_energy_excludes_design_and_warmup(sql_directory):
    metrics = SQLiteResultParser(OutputConfig()).parse(sql_directory)
    assert metrics.annual_heating_kwh == pytest.approx(1000)
    assert metrics.annual_cooling_kwh == pytest.approx(2000)
    assert metrics.annual_total_hvac_kwh is None
    assert metrics.peak_heating_kw is None


def test_explicit_total_and_peak(sql_directory):
    execute(sql_directory, '''
        INSERT INTO ReportDataDictionary VALUES (3, 0, 'Avg', 'SYSTEM', 'Verified Test Load Rate', 'Hourly', 'W');
        INSERT INTO "Time" VALUES (4, 1, 0, 1), (5, 1, 0, 365);
        INSERT INTO ReportData VALUES (5, 4, 3, 1200), (6, 5, 3, 3200), (7, 2, 3, 999999);
    ''')
    config = OutputConfig(total_hvac_meters=("Heating:Electricity", "Cooling:Electricity"), peak_heating=PeakVariable("Verified Test Load Rate", "SYSTEM"))
    result = SQLiteResultParser(config).parse(sql_directory)
    assert result.peak_heating_kw == pytest.approx(3.2)
    assert result.annual_total_hvac_kwh == pytest.approx(3000)


@pytest.mark.parametrize("statement, message", [
    ('DELETE FROM ReportDataDictionary WHERE ReportDataDictionaryIndex=1', "Expected exactly one"),
    ('UPDATE "Time" SET SimulationDays=30 WHERE TimeIndex=1', "full-year"),
    ("UPDATE ReportDataDictionary SET Units='W' WHERE ReportDataDictionaryIndex=1", "Unsupported units"),
    ('UPDATE ReportData SET Value=-1 WHERE ReportDataIndex=1', "Invalid negative"),
    ('UPDATE ReportData SET Value=NULL WHERE ReportDataIndex=1', "Invalid negative"),
    ('INSERT INTO EnvironmentPeriods VALUES (3, 3)', "one weather"),
    ('INSERT INTO ReportData VALUES (5, 1, 1, 100)', "full-year"),
    ("INSERT INTO ReportDataDictionary VALUES (4, 1, 'Sum', '', 'Heating:Electricity', 'Run Period', 'J')", "Expected exactly one"),
])
def test_invalid_sql_fails_cleanly(sql_directory, statement, message):
    execute(sql_directory, statement)
    with pytest.raises(ResultParsingError, match=message):
        SQLiteResultParser(OutputConfig()).parse(sql_directory)


def test_leap_year_and_different_units(sql_directory):
    execute(sql_directory, '''UPDATE "Time" SET SimulationDays=366 WHERE TimeIndex=1;
        UPDATE ReportDataDictionary SET Units='GJ' WHERE ReportDataDictionaryIndex=1;
        UPDATE ReportData SET Value=3.6 WHERE ReportDataIndex=1;''')
    assert SQLiteResultParser(OutputConfig()).parse(sql_directory).annual_heating_kwh == pytest.approx(1000)


def test_missing_sql_is_not_created(tmp_path):
    with pytest.raises(ResultParsingError, match="Missing"):
        SQLiteResultParser(OutputConfig()).parse(tmp_path)
    assert not (tmp_path / "eplusout.sql").exists()


def test_bad_sql_schema(tmp_path):
    (tmp_path / "eplusout.sql").write_text("not a database")
    with pytest.raises(ResultParsingError, match="Cannot parse"):
        SQLiteResultParser(OutputConfig()).parse(tmp_path)


def test_duplicate_meter_config_rejected():
    with pytest.raises(ValueError, match="double-count"):
        replace(OutputConfig(), heating_meters=("Heating:Electricity", "heating:electricity"))
