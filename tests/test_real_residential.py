"""Opt-in actual EnergyPlus execution; preserve artifacts under data/, not temporary test folders."""
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.energyplus.installation import detect_energyplus
from thermal_home_modeling.energyplus.residential_model import load_v0_configuration, LOAD_VARIABLES
from thermal_home_modeling.experiments.residential import generate_residential_cases, resolve_residential_weather, run_residential_batch


@pytest.mark.skipif(os.getenv('THM_RUN_REAL_RESIDENTIAL')!='1', reason='Opt-in actual installed EnergyPlus residential pilot')
def test_actual_eight_case_residential_pilot():
    installation=detect_energyplus()
    mapping,_=load_v0_configuration()
    weather=resolve_residential_weather(installation,mapping)
    root=Path(__file__).resolve().parents[1]/'data'/'residential_integration'/uuid4().hex
    paths=MVPPaths(root/'input',root/'output')
    outcome=run_residential_batch(generate_residential_cases(weather), mode='parallel', workers=4,
                                  installation=installation, paths=paths, reuse=False)
    assert outcome.summary['succeeded']==8 and outcome.summary['failed']==0
    assert outcome.frame.input_signature.nunique()==8
    assert outcome.frame.output_directory.nunique()==8
    for row in outcome.frame.itertuples():
        raw=Path(row.output_directory)
        for name in ['eplusout.sql','eplusout.err','eplusout.end','eplusout.rdd']:
            assert (raw/name).is_file()
        assert 'Severe' not in '\n'.join(x for x in (raw/'eplusout.err').read_text().splitlines() if '** Severe **' in x)
        rdd=(raw/'eplusout.rdd').read_text()
        assert all(name in rdd for name in LOAD_VARIABLES.values())
        with sqlite3.connect(raw/'eplusout.sql') as c:
            for name in LOAD_VARIABLES.values():
                value,units,freq=c.execute('SELECT r.Value,d.Units,d.ReportingFrequency FROM ReportData r JOIN ReportDataDictionary d USING(ReportDataDictionaryIndex) WHERE d.Name=? AND d.ReportingFrequency=?', (name,'Run Period')).fetchone()
                assert value>=0 and units=='J' and freq=='Run Period'
            count,low,high=c.execute("SELECT count(*),min(r.Value),max(r.Value) FROM ReportData r JOIN ReportDataDictionary d USING(ReportDataDictionaryIndex) WHERE d.Name='Zone Mean Air Temperature'").fetchone()
            assert count==8760 and low>=19.99 and high<=26.01
        model=json.loads(Path(row.generated_input).read_text())
        assert model['Building']['Residential V0']['north_axis']==row.orientation_deg
        assert model['BuildingSurface:Detailed']['South']['construction_name']=='Wall '+row.wall_id
    (root/'validation.json').write_text(json.dumps(outcome.summary,indent=2))
    print('\nRetained integration artifacts:',root)
