"""Small, explicitly artificial epJSON/SQLite assets for the smoke workflow."""

from pathlib import Path
import sqlite3

from thermal_home_modeling.config.experiment_config import phoenix_example
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.utilities.files import write_json
from thermal_home_modeling.utilities.paths import ProjectPaths


def create_settings(root: Path) -> Settings:
    """Create synthetic assets in an isolated root, with no real EPW or building."""
    paths = ProjectPaths.from_root(root)
    paths.ensure_directories()
    baseline = paths.baseline_models / "synthetic.epJSON"
    metadata = paths.baseline_models / "synthetic_parameters.json"
    write_json(baseline, {
        "Building": {"SYNTHETIC - NOT A VALID BUILDING": {"north_axis": 0}},
        "GlobalGeometryRules": {"Rules": {"coordinate_system": "Relative", "starting_vertex_position": "UpperLeftCorner", "vertex_entry_direction": "Counterclockwise"}},
    })
    write_json(metadata, phoenix_example().parameters())
    (paths.weather / "phoenix.epw").write_text("SYNTHETIC SMOKE PLACEHOLDER - NOT WEATHER DATA\n", encoding="utf-8")
    return Settings(paths=paths, baseline_model=baseline, baseline_parameters=metadata)


def write_synthetic_sql(path: Path, *, days: int = 365) -> None:
    """Write only the documented SQL columns consumed by the initial parser.

    Fixed 1000/2000 kWh values exercise unit conversion; they have no physical
    relationship to the input and must never be used for engineering analysis.
    """
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE EnvironmentPeriods (EnvironmentPeriodIndex INTEGER PRIMARY KEY, EnvironmentType INTEGER);
            CREATE TABLE ReportDataDictionary (ReportDataDictionaryIndex INTEGER PRIMARY KEY,
                IsMeter INTEGER, Type TEXT, KeyValue TEXT, Name TEXT, ReportingFrequency TEXT, Units TEXT);
            CREATE TABLE "Time" (TimeIndex INTEGER PRIMARY KEY, EnvironmentPeriodIndex INTEGER,
                WarmupFlag INTEGER, SimulationDays INTEGER);
            CREATE TABLE ReportData (ReportDataIndex INTEGER PRIMARY KEY, TimeIndex INTEGER,
                ReportDataDictionaryIndex INTEGER, Value REAL);
            INSERT INTO EnvironmentPeriods VALUES (1, 3), (2, 1);
            INSERT INTO ReportDataDictionary VALUES (1, 1, 'Sum', '', 'Heating:Electricity', 'Run Period', 'J');
            INSERT INTO ReportDataDictionary VALUES (2, 1, 'Sum', '', 'Cooling:Electricity', 'Run Period', 'J');
            INSERT INTO "Time" VALUES (2, 2, 0, 1), (3, 1, 1, 1);
            INSERT INTO ReportData VALUES (1, 1, 1, 3600000000), (2, 1, 2, 7200000000);
            INSERT INTO ReportData VALUES (3, 2, 1, 999999999999), (4, 3, 1, 999999999999);
        ''')
        connection.execute('INSERT INTO "Time" VALUES (1, 1, 0, ?)', (days,))
