"""Flexible physical experiments, deterministic sampling, constraints and durable jobs."""
from copy import deepcopy
from dataclasses import replace
import fcntl
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_residential import residential, demo
from thermal_home_modeling.energyplus.residential_model import generate_residential_model
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.experiments.residential import prepare_residential_cases, run_residential_batch, compare_residential_timing
from thermal_home_modeling.experiments.residential_design import default_design, generate_design, physical_identity
from thermal_home_modeling.experiments import campaign_jobs
from thermal_home_modeling.utilities.files import fingerprint


@pytest.fixture
def design_env(residential):
    args,runner,cases=residential
    weather={}
    for city,b in args['mappings']['weather_bindings'].items():
        path=cases[0].weather_file.parent/(city+'.epw')
        path.write_text(f"LOCATION,{b['station_name']},{b['region']},{b['country']},TMY3,{b['station_id']},{b['latitude']},{b['longitude']},-5,82\n")
        weather[city]=path
    def design(spec=None):
        return generate_design(spec or default_design(args['catalog'],args['mappings']), args['catalog'],args['mappings'],args['assumptions'],args['installation'],weather)
    return args,runner,weather,design


def test_all_six_cities_full_factorial_unique_count(design_env):
    args,_,weather,design=design_env
    full=design()
    assert full.available==len(full.cases)==1728
    assert full.excluded==432
    assert {c.city_id for c in full.cases}==set(args['catalog']['locations'])
    assert len({fingerprint(c.to_dict()) for c in full.cases})==1728
    for c in full.cases:
        assert c.weather_file==weather[c.city_id]
        assert c.attic_r_ip in args['catalog']['locations'][c.city_id]['value']['attic_levels_r_ip']
        assert c.city_id in args['catalog']['wall_assemblies'][c.wall_id]['value']['eligible_cities']


@pytest.mark.parametrize('count',[50,100,1000])
def test_seeded_fixed_count_sampling(design_env,count):
    args,_,_,design=design_env
    spec={**default_design(args['catalog'],args['mappings']),'sampling':'random','count':count,'seed':42}
    a,b=design(spec),design(spec)
    assert len(a.cases)==count and a.cases==b.cases and a.identity==b.identity
    assert len({fingerprint(c.to_dict()) for c in a.cases})==count
    spec['seed']=43
    assert design(spec).cases!=a.cases


def test_balanced_marginals_and_constraints(design_env):
    args,_,_,design=design_env
    spec={**default_design(args['catalog'],args['mappings']),'sampling':'balanced','count':12,'seed':42}
    result=design(spec)
    assert set(result.counts['city_id'].values())=={2}
    assert set(result.counts['house_id'].values())=={4}
    assert set(result.counts['wall_id'].values())=={2}
    assert set(result.counts['orientation_deg'].values())=={3}
    assert design(spec).cases==result.cases
    spec['constraints']={'exclude':[{'city_id':'phoenix','attic_r_ip':80},{'house_id':'1L','wall_id':'W9'}]}
    result=design(spec)
    assert result.available<1728
    assert not any(c.city_id=='phoenix' and c.attic_r_ip==80 for c in result.cases)
    assert not any(c.house_id=='1L' and c.wall_id=='W9' for c in result.cases)


@pytest.mark.parametrize('bad',[
 {'fixed':{'glazing':'triple'}}, {'parameters':{'city_id':['chicago']}},
 {'parameters':{'house_id':['2S']}}, {'sampling':'random','count':5000},
 {'sampling':'random','count':0}, {'sampling':'random','count':2.5},
 {'sampling':'random','count':True}, {'sampling':'random'},
 {'sampling':'full_factorial','count':2}, {'constraints':{'expression':'1+1'}},
 {'constraints':{'exclude':[{'wwr':.6}]}}, {'parameters':{'wall_id':[]}},
 {'fixed':{'wall_id':'W3'},'parameters':{'wall_id':['W9']}},
])
def test_bad_specs_rejected(design_env,bad):
    args,_,_,design=design_env
    with pytest.raises(ConfigurationError):design(bad)


def test_orientation_symmetry_and_integer_float_dedup(design_env):
    args,_,weather,design=design_env
    result=design({'parameters':{'orientation_deg':[0,0.0,90,180,270]}})
    assert result.available==2 and result.duplicates==2
    assert {c.orientation_deg for c in result.cases}=={0,90}
    plans=prepare_residential_cases([*result.cases,replace(result.cases[0],orientation_deg=result.cases[0].orientation_deg+180)],
        args['catalog'],args['mappings'],args['assumptions'],args['installation'],args['paths'])
    assert len(plans)==2


def test_same_physics_with_different_wall_labels_deduplicates(design_env):
    args,_,_,design=design_env
    wall=deepcopy(args['catalog']['wall_assemblies']['W3'])
    m=wall['value']['mapping'];m['construction_name']='Wall W99'
    m['layers']=[v.replace('W3','W99') for v in m['layers']]
    m['materials']={k.replace('W3','W99'):v for k,v in m['materials'].items()}
    args['catalog']['wall_assemblies']['W99']=wall
    args['mappings']['reviewed_wall_hashes']['W99']=fingerprint(m)
    result=design({'parameters':{'wall_id':['W3','W99']}})
    assert result.available==1 and result.duplicates==1


def test_house_geometry_really_scales_floor_walls_windows(design_env):
    args,_,_,design=design_env
    cases=design({'parameters':{'house_id':['1S','1M','1L']}}).cases
    hashes=set()
    for case in cases:
        model=generate_residential_model(case,args['catalog'],args['mappings'],args['assumptions'],'26.2')
        zone=model['Zone']['Residential Zone'];area=args['catalog']['houses'][case.house_id]['value']['floor_area_ft2']*.09290304
        assert zone['floor_area']==pytest.approx(area)
        assert zone['volume']==pytest.approx(area*2.4384)
        vertices=model['BuildingSurface:Detailed']['South']['vertices']
        wall_area=(max(v['vertex_x_coordinate'] for v in vertices)-min(v['vertex_x_coordinate'] for v in vertices))*2.4384
        win=model['FenestrationSurface:Detailed']['South window']
        xs=[win[f'vertex_{i}_x_coordinate'] for i in range(1,5)];zs=[win[f'vertex_{i}_z_coordinate'] for i in range(1,5)]
        assert (max(xs)-min(xs))*(max(zs)-min(zs))==pytest.approx(wall_area*.19)
        hashes.add(fingerprint(model))
    assert len(hashes)==3


def test_background_job_snapshot_lock_and_single_writer(design_env,monkeypatch):
    args,runner,_,design=design_env
    cases=design({'parameters':{'wall_id':['W3','W9']}}).cases
    popen=Mock()
    monkeypatch.setattr(campaign_jobs,'detect_energyplus',lambda *a:args['installation'])
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',popen)
        job=campaign_jobs.submit_campaign(cases,**args)
    assert popen.call_args.kwargs['start_new_session'] is True
    with (job/'worker.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert campaign_jobs.run_job(job)==2
    assert campaign_jobs.run_job(job)==0
    assert campaign_jobs.job_status(job)['state']=='complete'
    assert runner.run.call_count==2
    assert campaign_jobs.run_job(job)==2 and runner.run.call_count==2
    batch=Path(campaign_jobs.job_status(job)['batch_directory'])
    assert len(list((batch/'records').glob('*.json')))==2
    state=campaign_jobs.job_status(job)
    assert state['summary']['sum_energyplus_runtime_seconds']>0
    assert state['summary']['mean_energyplus_runtime_seconds']>0


def test_job_rejects_changed_effective_inputs_after_submission(design_env,monkeypatch):
    args,runner,weather,design=design_env
    cases=design({'fixed':{'city_id':'washington_dc'}}).cases
    monkeypatch.setattr(campaign_jobs,'detect_energyplus',lambda *a:args['installation'])
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',Mock())
        job=campaign_jobs.submit_campaign(cases,**args)
    weather['washington_dc'].write_text(weather['washington_dc'].read_text()+'changed content\n')
    assert campaign_jobs.run_job(job)==1
    assert 'changed after submission' in campaign_jobs.job_status(job)['error']
    assert runner.run.call_count==0


def test_worker_stop_checkpoint_and_resume(design_env,monkeypatch):
    args,runner,_,design=design_env
    cases=design({'parameters':{'wall_id':['W3','W9']}}).cases
    monkeypatch.setattr(campaign_jobs,'detect_energyplus',lambda *a:args['installation'])
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',Mock())
        job=campaign_jobs.submit_campaign(cases,**args)
    (job/'stop_requested').touch()
    assert campaign_jobs.run_job(job)==1
    status=campaign_jobs.job_status(job)
    assert status['state']=='interrupted'
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',Mock())
        resumed=campaign_jobs.submit_resume(status['batch_directory'],**{k:v for k,v in args.items() if k not in ('catalog','mappings','assumptions')})
    assert campaign_jobs.run_job(resumed)==0
    assert runner.run.call_count==2


def test_benchmark_rejects_cache_or_different_case_sets(design_env):
    args,_,_,design=design_env
    cases=design({'parameters':{'wall_id':['W3','W9']}}).cases
    a=run_residential_batch(cases,mode='sequential',workers=1,reuse=False,**args)
    b=run_residential_batch(cases,mode='parallel',workers=4,reuse=False,**args)
    assert compare_residential_timing(a.summary,b.summary)['first_over_second_wall_time']>0
    cached=run_residential_batch(cases,**args)
    assert cached.summary['cached_cases']==2 and cached.summary['mean_energyplus_runtime_seconds'] is None
    with pytest.raises(ConfigurationError):compare_residential_timing(a.summary,cached.summary)
    with pytest.raises(ConfigurationError):compare_residential_timing(a.summary,{**b.summary,'case_set_signature':'different'})


def test_cli_and_streamlit_share_design_and_dry_run_does_not_execute(design_env,monkeypatch,tmp_path):
    from thermal_home_modeling import cli
    import thermal_home_modeling.experiments.residential as batch
    args,runner,weather,design=design_env
    spec={**default_design(args['catalog'],args['mappings']), 'sampling':'random','count':8,'seed':92,
          'weather_files':{k:str(v) for k,v in weather.items()}}
    path=tmp_path/'experiment.json';path.write_text(json.dumps(spec))
    monkeypatch.setattr('thermal_home_modeling.energyplus.installation.detect_energyplus',lambda *a:args['installation'])
    monkeypatch.setattr(batch.MVPPaths,'default',lambda:args['paths'])
    execute=Mock()
    monkeypatch.setattr(batch,'run_residential_batch',execute)
    assert cli.main(['residential-batch','--config',str(path),'--dry-run','--fresh'])==0
    assert execute.call_count==runner.run.call_count==0
    submit=Mock(return_value=tmp_path/'job')
    monkeypatch.setattr(campaign_jobs,'submit_campaign',submit)
    assert cli.main(['residential-batch','--config',str(path),'--background','--fresh','--workers','8'])==0
    assert submit.call_args.args[0]==design(spec).cases
    assert submit.call_args.kwargs['workers']==8 and submit.call_args.kwargs['reuse'] is False
    assert cli.main(['residential-batch','--config',str(path),'--count','5000','--dry-run'])==1
    assert execute.call_count==runner.run.call_count==0


def test_1000_case_submission_snapshots_without_launching_simulator(design_env,monkeypatch):
    args,runner,_,design=design_env
    spec={**default_design(args['catalog'],args['mappings']),'sampling':'random','count':1000,'seed':42}
    selected=design(spec)
    monkeypatch.setattr(campaign_jobs.subprocess,'Popen',Mock())
    job=campaign_jobs.submit_campaign(selected.cases,experiment=selected.summary(),**args)
    request=json.loads((job/'request.json').read_text())
    assert len(request['cases'])==len(set(request['input_signatures']))==1000
    assert (job/'catalog.json').is_file() and (job/'assumptions.json').is_file()
    assert runner.run.call_count==0


def test_asymmetric_model_keeps_opposing_orientations(design_env,monkeypatch):
    from thermal_home_modeling.experiments import residential_design as module
    _,_,_,design=design_env
    original=module.generate_residential_model
    def asymmetric(*a,**kw):
        m=original(*a,**kw)
        m['FenestrationSurface:Detailed'].pop('South window')
        return m
    monkeypatch.setattr(module,'generate_residential_model',asymmetric)
    result=design({'parameters':{'orientation_deg':[0,90,180,270]}})
    assert result.available==4 and result.duplicates==0


def test_canonical_orientation_still_obeys_exclusions(design_env):
    _,_,_,design=design_env
    with pytest.raises(ConfigurationError,match='No valid configurations'):
        design({'parameters':{'orientation_deg':[180]},'constraints':{'exclude':[{'orientation_deg':0}]}})


def test_retry_job_before_batch_exists_uses_frozen_submission(design_env,monkeypatch):
    args,runner,_,design=design_env
    cases=design({'fixed':{'city_id':'washington_dc'}}).cases
    monkeypatch.setattr(campaign_jobs,'detect_energyplus',lambda *a:args['installation'])
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',Mock())
        job=campaign_jobs.submit_campaign(cases,**args)
    state=json.loads((job/'status.json').read_text());state['state']='failed'
    (job/'status.json').write_text(json.dumps(state))
    with monkeypatch.context() as patch:
        patch.setattr(campaign_jobs.subprocess,'Popen',Mock())
        resumed=campaign_jobs.retry_job(job,**{k:v for k,v in args.items() if k not in ('catalog','mappings','assumptions')})
    assert campaign_jobs.run_job(resumed)==0 and runner.run.call_count==1
    # A stale running record is recognized via the released kernel lock.
    state=json.loads((resumed/'status.json').read_text());state['state']='running'
    (resumed/'status.json').write_text(json.dumps(state))
    assert campaign_jobs.job_status(resumed)['state']=='interrupted'


def test_weather_fetch_checks_hash_and_preserves_existing_file(tmp_path,monkeypatch):
    from io import BytesIO
    from zipfile import ZipFile
    import hashlib
    from thermal_home_modeling.weather import research
    payload=b'LOCATION,Phoenix-Sky Harbor Intl AP,AZ,USA,TMY3,722780,33.45,-111.983,-7,337\n'
    archive=BytesIO()
    with ZipFile(archive,'w') as z:z.writestr('Phoenix.epw',payload)
    data=archive.getvalue()
    mappings={'weather_bindings':{'phoenix':{'source_url':'https://example.test/Phoenix.zip','file_name':'Phoenix.epw',
        'epw_sha256':hashlib.sha256(payload).hexdigest(),'archive_sha256':hashlib.sha256(data).hexdigest()}}}
    monkeypatch.setattr(research,'load_v0_configuration',lambda:(mappings,{}))
    monkeypatch.setattr(research,'CATALOG_ROOT',tmp_path/'catalogs')
    download=Mock(side_effect=lambda *a,**kw:BytesIO(data))
    monkeypatch.setattr(research,'urlopen',download)
    files=research.fetch_research_weather()
    assert Path(files[0]).read_bytes()==payload
    assert research.fetch_research_weather()==files and download.call_count==1
    Path(files[0]).write_bytes(b'changed')
    with pytest.raises(ConfigurationError,match='retained unchanged'):research.fetch_research_weather()
    assert Path(files[0]).read_bytes()==b'changed'
