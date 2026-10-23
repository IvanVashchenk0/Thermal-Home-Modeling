"""Residential catalog/physical mappings, orchestration, recovery and UI contracts."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
from unittest.mock import Mock
from zipfile import ZipFile

import pandas as pd
import pytest

from test_mvp_reuse_and_ui import demo
from thermal_home_modeling.catalogs.workbook import CATALOG_ROOT, import_catalog, load_catalog
from thermal_home_modeling.energyplus.residential_model import generate_residential_model, load_v0_configuration, symmetric_180, LOAD_VARIABLES
from thermal_home_modeling.energyplus.result_parser import SQLiteResultParser
from thermal_home_modeling.energyplus.runner import EnergyPlusRunner
from thermal_home_modeling.config.output_config import OutputConfig
from thermal_home_modeling.errors import ConfigurationError, ResultParsingError
from thermal_home_modeling.experiments.residential import generate_residential_cases, prepare_residential_cases, run_residential_batch, resume_residential_batch
from thermal_home_modeling.utilities.files import fingerprint


@pytest.fixture
def residential(demo, monkeypatch):
    args, runner = demo
    weather = args['installation'].weather_file
    weather.write_text('LOCATION,Washington Dc Dulles IntL Ar,VA,USA,TMY3,724030,38.98,-77.47,-5,82\n')
    args['installation'] = replace(args['installation'], version='26.2.0-test')
    args['catalog'] = load_catalog()
    args['mappings'], args['assumptions'] = load_v0_configuration()
    original = runner.run
    def emit(*a, **kw):
        execution = original(*a, **kw)
        with sqlite3.connect(Path(a[2]) / 'eplusout.sql') as c:
            c.execute('DELETE FROM ReportData')
            c.execute('DELETE FROM ReportDataDictionary')
            for n, name in enumerate(LOAD_VARIABLES.values(), 1):
                c.execute("INSERT INTO ReportDataDictionary VALUES (?,0,'Sum','IDEAL LOADS',?,'Run Period','J')", (n, name))
                c.execute('INSERT INTO ReportData VALUES (?,1,?,?)', (n, n, n*12345.0))
        return execution
    runner.run = Mock(side_effect=emit)
    monkeypatch.setattr('thermal_home_modeling.experiments.residential.EnergyPlusRunner', Mock(return_value=runner, _check_completion=EnergyPlusRunner._check_completion))
    monkeypatch.setattr('thermal_home_modeling.experiments.mvp_batch.available_cpus', lambda: 4)
    return args, runner, generate_residential_cases(weather)


def test_published_catalog_is_complete():
    catalog = load_catalog()
    assert len(catalog['sheets']) == 16
    assert len(catalog['locations']) == 6 and len(catalog['houses']) == 6 and len(catalog['wall_assemblies']) == 13
    assert catalog['planning_count'] == 115200
    assert catalog['houses']['1S']['value']['footprint'] == 'TBD'
    assert any(c['formula'] for s in catalog['sheets'].values() for c in s['cells'].values())


def test_repeatable_import_with_optional_source_workbook(tmp_path):
    catalog = load_catalog()
    source = CATALOG_ROOT/'revisions'/catalog['workbook_sha256']/'source.xlsx'
    if not source.is_file():
        pytest.skip('Workbook import round-trip requires the locally retained source.xlsx')
    report = import_catalog(source, tmp_path)
    marker = tmp_path/'residential_mappings.json'
    marker.write_text('reviewed mapping sentinel')
    assert report['changed_cells']
    assert not import_catalog(source, tmp_path)['changed_cells']
    assert marker.read_text() == 'reviewed mapping sentinel'
    # Edit a literal XML note while preserving formula caches, unlike openpyxl saves.
    changed = tmp_path/'changed.xlsx'
    with ZipFile(source) as old, ZipFile(changed, 'w') as new:
        for member in old.infolist():
            data = old.read(member.filename)
            if member.filename == 'xl/sharedStrings.xml':
                data = data.replace(b'Thermal Home', b'Edited Thermal Home', 1)
            new.writestr(member, data)
    changed_report = import_catalog(changed, tmp_path)
    assert changed_report['changed_cells']
    assert marker.read_text() == 'reviewed mapping sentinel'


def test_eight_models_real_wall_roof_orientation_changes(residential):
    args, _, cases = residential
    plans = prepare_residential_cases(cases, args['catalog'], args['mappings'], args['assumptions'], args['installation'], args['paths'])
    assert len(plans) == len({p['signature'] for p in plans}) == 8
    assert {(c.wall_id, c.attic_r_ip, c.orientation_deg) for c in cases} == {(w,r,o) for w in ['W3','W9'] for r in [49,80] for o in [0,90]}
    for plan in plans:
        m,c = plan['model'],plan['case']
        assert m['Building']['Residential V0']['north_axis'] == c.orientation_deg
        assert m['Material:NoMass']['Attic insulation']['thermal_resistance'] == pytest.approx(c.attic_r_ip*.1761101838)
        assert m['BuildingSurface:Detailed']['South']['construction_name'] == 'Wall '+c.wall_id
        material = m['Material'][c.wall_id+' Cavity Equivalent']
        assert material['thickness'] == pytest.approx(.0889 if c.wall_id == 'W3' else .1397)
        assert material['conductivity'] == pytest.approx(.050257393242687264 if c.wall_id == 'W3' else .036397280091671029)
        assert m['Construction']['Wall '+c.wall_id]['outside_layer'] == 'Vinyl Siding'
        assert m['Construction']['Wall '+c.wall_id]['layer_2'] == 'OSB 7/16'
        assert len(m['BuildingSurface:Detailed']) == 6 and len(m['FenestrationSurface:Detailed']) == 4
        assert plan['symmetry_180_verified']
        assert not any('film' in name.lower() for name in m['Material'])


@pytest.mark.parametrize('change', ['window','shade','zone','wall'])
def test_symmetry_rejects_asymmetry(residential, change):
    args,_,cases = residential
    m = generate_residential_model(cases[0], args['catalog'], args['mappings'], args['assumptions'], args['installation'].version)
    if change == 'window': m['FenestrationSurface:Detailed'].pop('South window')
    if change == 'shade': m['Shading:Building:Detailed'] = {'Overhang': {}}
    if change == 'zone': m['Zone']['Other zone'] = {}
    if change == 'wall': m['BuildingSurface:Detailed']['South']['construction_name'] = 'Different wall'
    assert not symmetric_180(m)


@pytest.mark.parametrize('field,value', [('city_id','phoenix'),('house_id','2S'),('wall_id','W1'),('attic_r_ip',38),('orientation_deg',17)])
def test_unsupported_inputs_rejected(residential, field, value):
    args,_,cases = residential
    with pytest.raises(ConfigurationError):
        generate_residential_model(replace(cases[0], **{field:value}), args['catalog'], args['mappings'], args['assumptions'], '26.2')
    with pytest.raises(ConfigurationError, match='unsupported'):
        generate_residential_cases(cases[0].weather_file, glazing='triple')


def test_wrong_weather_and_changed_physical_definition_blocked(residential):
    args,_,cases = residential
    args['catalog']['wall_assemblies']['W3']['value']['mapping']['materials']['W3 Cavity Equivalent']['properties']['conductivity'] *= 2
    with pytest.raises(ConfigurationError, match='changed'):
        generate_residential_model(cases[0], args['catalog'], args['mappings'], args['assumptions'], '26.2')
    args['catalog'] = load_catalog()
    cases[0].weather_file.write_text('LOCATION,Chicago,IL,USA,TMY3,123,41,-87,-6,100\n')
    with pytest.raises(ConfigurationError, match='Weather'):
        generate_residential_model(cases[0], args['catalog'], args['mappings'], args['assumptions'], '26.2')


def test_description_does_not_change_effective_identity(residential):
    args,_,cases = residential
    def plan(): return prepare_residential_cases(cases[:1], args['catalog'], args['mappings'], args['assumptions'], args['installation'], args['paths'])[0]['signature']
    before=plan()
    args['catalog']['workbook_sha256']='new-revision'
    args['catalog']['wall_assemblies']['W3']['value']['description']='Edited note'
    assert plan()==before
    args['assumptions']['operation']['heating_setpoint_c']=21
    assert plan()!=before


@pytest.mark.parametrize('mode,concurrency', [('sequential',1),('parallel',4)])
def test_batch_isolation_exports_cache_and_resume(residential, mode, concurrency):
    args,runner,cases=residential
    caller=threading.get_ident()
    original=runner.run
    barrier=threading.Barrier(concurrency)
    def synchronized(*a, **kw):
        barrier.wait(timeout=10)
        return original(*a, **kw)
    runner.run=Mock(side_effect=synchronized)
    progress=[]
    def callback(p):
        assert threading.get_ident()==caller
        progress.append(p)
    first=run_residential_batch(cases, mode=mode, workers=4, on_progress=callback, **args)
    assert first.summary['succeeded']==8 and first.summary['failed']==0
    assert first.frame.generated_input.nunique()==first.frame.output_directory.nunique()==8
    assert max(p.running for p in progress)==concurrency
    assert len(pd.read_parquet(first.directory/'batch_results.parquet'))==8
    assert set(first.frame.heating_load_j_units)=={'J'}
    # Resume snapshots, not the current import; no new subprocesses.
    cached=resume_residential_batch(first.directory, **{k:v for k,v in args.items() if k not in ('catalog','mappings','assumptions')})
    assert cached.summary['cached_cases']==8 and cached.summary['newly_executed_cases']==0
    assert runner.run.call_count==8
    assert set(cached.frame.simulation_id)==set(first.frame.simulation_id)
    assert json.loads((cached.directory/'batch_manifest.json').read_text())['parent_batch']==first.batch_id


@pytest.mark.parametrize('damaged', ['sql','model','result','end'])
def test_corrupt_cache_is_not_reused(residential, damaged):
    args,runner,cases=residential
    first=run_residential_batch(cases[:1], **args)
    row=first.frame.iloc[0]
    if damaged=='sql': (Path(row.output_directory)/'eplusout.sql').write_bytes(b'broken')
    if damaged=='model': Path(row.generated_input).write_text('{}')
    if damaged=='end': (Path(row.output_directory)/'eplusout.end').write_text('failed')
    if damaged=='result':
        p=Path(row.result_path); r=json.loads(p.read_text()); r['outputs']['heating_load_j']['value']=999; p.write_text(json.dumps(r))
    fresh=run_residential_batch(cases[:1], **args)
    assert fresh.summary['cached_cases']==0 and runner.run.call_count==2


def test_failure_isolation_and_partial_resume(residential):
    args,runner,cases=residential
    original=runner.run
    def fail_one(model,*a,**kw):
        m=json.loads(model.read_text())
        if m['Building']['Residential V0']['north_axis']==90:
            raise RuntimeError('injected case failure')
        return original(model,*a,**kw)
    runner.run=Mock(side_effect=fail_one)
    first=run_residential_batch(cases[:2], **args)
    assert first.summary['failed']==1 and first.summary['succeeded']==1
    assert pd.isna(first.frame.loc[~first.frame.success,'heating_load_j'].iloc[0])
    runner.run=original
    second=resume_residential_batch(first.directory, **{k:v for k,v in args.items() if k not in ('catalog','mappings','assumptions')})
    assert second.summary['cached_cases']==1 and second.summary['newly_executed_cases']==1 and second.summary['failed']==0


def test_parser_keeps_original_joules_and_rejects_wrong_units(residential):
    args,_,cases=residential
    batch=run_residential_batch(cases[:1], **args)
    raw=Path(batch.frame.iloc[0].output_directory)
    parser=SQLiteResultParser(OutputConfig())
    values=parser.parse_raw_annual_variables(raw,LOAD_VARIABLES,key='IDEAL LOADS')
    assert values['heating_load_j']['value']==12345 and values['heating_load_j']['units']=='J'
    with sqlite3.connect(raw/'eplusout.sql') as c: c.execute("UPDATE ReportDataDictionary SET Units='kWh'")
    with pytest.raises(ResultParsingError): parser.parse_raw_annual_variables(raw,LOAD_VARIABLES,key='IDEAL LOADS')


def test_residential_viewer_reads_saved_results_without_engine(residential, monkeypatch):
    from streamlit.testing.v1 import AppTest
    args,runner,cases=residential
    import thermal_home_modeling.presentation.streamlit_residential as ui
    monkeypatch.setattr(ui.MVPPaths, 'default', lambda: args['paths'])
    run_residential_batch(cases[:2], **args)
    before=runner.run.call_count
    def forbidden(*a, **k):
        raise AssertionError('Viewer must not detect or launch EnergyPlus')
    monkeypatch.setattr('thermal_home_modeling.energyplus.installation.detect_energyplus', forbidden)
    app=AppTest.from_file(str(Path(__file__).parents[1]/'streamlit_app.py')).run(timeout=20)
    assert not app.exception
    assert not app.button
    assert any(x.label=='Campaign' for x in app.selectbox)
    assert any(x.label=='Successful' for x in app.metric)
    app.multiselect(key='filter_wall_id').set_value(['W3']).run(timeout=20)
    assert not app.exception
    assert runner.run.call_count==before


def test_interruption_preserves_running_case_records_for_resume(residential):
    args,runner,cases=residential
    def interrupt(p):
        if p.completed==1:
            raise KeyboardInterrupt('test interruption')
    with pytest.raises(KeyboardInterrupt):
        run_residential_batch(cases, on_progress=interrupt, **args)
    directory=next(args['paths'].output.glob('batches/residential_*'))
    assert json.loads((directory/'batch_manifest.json').read_text())['status']=='interrupted'
    restored=resume_residential_batch(directory, **{k:v for k,v in args.items() if k not in ('catalog','mappings','assumptions')})
    assert restored.summary['succeeded']==8 and restored.summary['cached_cases']>=1
    assert runner.run.call_count==8
