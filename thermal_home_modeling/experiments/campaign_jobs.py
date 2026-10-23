"""Durable local CLI campaign jobs protected by a file lock."""
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from thermal_home_modeling.catalogs.workbook import load_catalog
from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.config.settings import Settings
from thermal_home_modeling.energyplus.installation import detect_energyplus
from thermal_home_modeling.energyplus.residential_model import load_v0_configuration
from thermal_home_modeling.experiments.residential import prepare_residential_cases, run_residential_batch
from thermal_home_modeling.models.residential import ResidentialInput
from thermal_home_modeling.utilities.files import write_json
from thermal_home_modeling.errors import ConfigurationError


def submit_campaign(cases, *, mode='parallel', workers=4, reuse=True, experiment=None,
                    settings=None, installation=None, paths=None, catalog=None, mappings=None,
                    assumptions=None, parent_batch=None):
    settings=settings or Settings.from_env()
    installation=installation or detect_energyplus(settings.energyplus_exe)
    paths=paths or MVPPaths.default()
    catalog=catalog or load_catalog()
    dm,da=load_v0_configuration();mappings=mappings or dm;assumptions=assumptions or da
    plans=prepare_residential_cases(cases,catalog,mappings,assumptions,installation,paths,reuse=False)
    if not plans: raise ConfigurationError('No valid cases to submit')
    job=paths.output/'jobs'/('job_'+uuid4().hex)
    job.mkdir(parents=True,exist_ok=False)
    batch=paths.output/'batches'/('residential_'+uuid4().hex)
    request={'schema_version':1,'cases':[p['case'].to_dict() for p in plans],
             'input_signatures':[p['signature'] for p in plans], 'mode':mode,'workers':workers,'reuse':reuse,
             'input_root':str(paths.input.resolve()),'output_root':str(paths.output.resolve()),
             'energyplus_exe':str(installation.executable),'timeout_seconds':settings.timeout_seconds,
             'experiment':experiment,'parent_batch':parent_batch,'batch_directory':str(batch.resolve())}
    for name,obj in [('request',request),('catalog',catalog),('mappings',mappings),('assumptions',assumptions)]:
        write_json(job/(name+'.json'),obj)
    write_json(job/'status.json',{'state':'queued','job_directory':str(job),'batch_directory':str(batch),
                                 'submitted_at':datetime.now(timezone.utc).isoformat(),'total':len(plans)})
    try:
        with (job/'worker.log').open('ab') as log:
            process=subprocess.Popen([sys.executable,'-m','thermal_home_modeling.experiments.campaign_jobs',str(job.resolve())],
                cwd=Path(__file__).resolve().parents[2],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                start_new_session=True,close_fds=True)
        if isinstance(process.pid,int):
            write_json(job/'launcher.json',{'pid':process.pid})
    except OSError as exc:
        write_json(job/'status.json',{'state':'failed','error':str(exc),'batch_directory':str(batch)})
        raise
    return job


def submit_resume(batch, **options):
    batch=Path(batch)
    manifest=json.loads((batch/'batch_manifest.json').read_text())
    if manifest.get('workflow')!='residential_v0': raise ConfigurationError('Residential batch required')
    cases=[ResidentialInput(**{**row,'weather_file':Path(row['weather_file'])}) for row in manifest['cases']]
    return submit_campaign(cases,catalog=json.loads((batch/'catalog_snapshot.json').read_text()),
        mappings=json.loads((batch/'mapping_snapshot.json').read_text()),assumptions=json.loads((batch/'assumptions_snapshot.json').read_text()),
        experiment=json.loads((batch/'experiment.json').read_text()) if (batch/'experiment.json').exists() else None,
        parent_batch=manifest['batch_id'],**options)


def retry_job(job, **options):
    """Recover even a worker that stopped before creating its batch manifest."""
    job=Path(job)
    state=job_status(job)
    if state['state'] in ('queued','running'):
        raise ConfigurationError('Stop the existing worker before resuming its job')
    request=json.loads((job/'request.json').read_text())
    batch=Path(request['batch_directory'])
    if (batch/'batch_manifest.json').exists():
        return submit_resume(batch,**options)
    cases=[ResidentialInput(**{**row,'weather_file':Path(row['weather_file'])}) for row in request['cases']]
    return submit_campaign(cases,catalog=json.loads((job/'catalog.json').read_text()),
        mappings=json.loads((job/'mappings.json').read_text()),assumptions=json.loads((job/'assumptions.json').read_text()),
        experiment=request.get('experiment'),parent_batch=request.get('parent_batch'),**options)


def job_status(job):
    job=Path(job)
    state=json.loads((job/'status.json').read_text())
    if state.get('state')=='queued' and (job/'launcher.json').exists():
        pid=json.loads((job/'launcher.json').read_text())['pid']
        try: os.kill(pid,0)
        except ProcessLookupError:
            state={**state,'state':'interrupted','error':'Worker exited before initialization; retry its saved job.'}
        except PermissionError:
            pass
    # A crashed worker releases the kernel lock; don't leave a permanent "running" badge.
    if state.get('state')=='running':
        with (job/'worker.lock').open('a+') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                state={**state,'state':'interrupted','error':'Worker exited without finalizing; resume its saved campaign.'}
                fcntl.flock(lock,fcntl.LOCK_UN)
    return state


def run_job(job):
    job=Path(job).resolve()
    with (job/'worker.lock').open('a+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return 2
        initial=json.loads((job/'status.json').read_text())
        if initial.get('state') not in ('queued',):
            return 2  # Resume creates a new attempt; never overwrite an old campaign.
        request=json.loads((job/'request.json').read_text())
        state={**initial,'state':'running','pid':os.getpid(),'started_at':datetime.now(timezone.utc).isoformat()}
        write_json(job/'status.json',state)
        last=0
        started=time.perf_counter()
        def progress(p):
            nonlocal last
            if (job/'stop_requested').exists():
                raise KeyboardInterrupt('Stop requested; active subprocess artifacts are retained')
            now=time.perf_counter()
            if now-last<.5 and p.completed<p.total:return
            last=now
            new_success=p.succeeded-p.cached
            rate=60*new_success/p.elapsed_seconds if p.elapsed_seconds else 0
            completed_rate=p.completed/p.elapsed_seconds if p.elapsed_seconds else 0
            state.update(completed=p.completed,total=p.total,running=p.running,successful=p.succeeded,failed=p.failed,
                         cached=p.cached,elapsed_seconds=now-started,new_successful_simulations_per_minute=rate,
                         estimated_remaining_seconds=(p.total-p.completed)/completed_rate if completed_rate else None,
                         heartbeat_at=datetime.now(timezone.utc).isoformat())
            write_json(job/'status.json',state)
        try:
            settings=replace(Settings.from_env(),energyplus_exe=request['energyplus_exe'],timeout_seconds=request['timeout_seconds'])
            installation=detect_energyplus(settings.energyplus_exe)
            paths=MVPPaths(Path(request['input_root']),Path(request['output_root']))
            catalog=json.loads((job/'catalog.json').read_text());mappings=json.loads((job/'mappings.json').read_text());assumptions=json.loads((job/'assumptions.json').read_text())
            cases=[ResidentialInput(**{**row,'weather_file':Path(row['weather_file'])}) for row in request['cases']]
            plans=prepare_residential_cases(cases,catalog,mappings,assumptions,installation,paths,reuse=False)
            if [p['signature'] for p in plans]!=request['input_signatures']:
                raise ConfigurationError('Effective inputs changed after submission (engine, weather or generator); create a new reviewed experiment')
            outcome=run_residential_batch(cases,mode=request['mode'],workers=request['workers'],reuse=request['reuse'],
                settings=settings,installation=installation,paths=paths,catalog=catalog,mappings=mappings,assumptions=assumptions,
                batch_directory=request['batch_directory'],experiment=request['experiment'],parent_batch=request['parent_batch'],on_progress=progress)
            state.update(state='complete' if not outcome.summary['failed'] else 'complete_with_failures',summary=outcome.summary,running=0)
            return_code=0 if not outcome.summary['failed'] else 1
        except BaseException as exc:
            state.update(state='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',error=f'{type(exc).__name__}: {exc}',running=0)
            return_code=1
        state.update(finished_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.perf_counter()-started)
        write_json(job/'status.json',state)
        return return_code


if __name__=='__main__':
    raise SystemExit(run_job(sys.argv[1]))
