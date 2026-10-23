"""Deterministic residential experiment design used by the CLI.

Only supported physical dimensions are accepted. Constraints are declarative
exclusions (never executable expressions). Sampling operates after validity and
physical/symmetry deduplication, so a requested count always means unique models.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from itertools import product
from pathlib import Path
import random

from thermal_home_modeling.energyplus.residential_model import generate_residential_model, symmetric_180
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.models.residential import ResidentialInput
from thermal_home_modeling.utilities.files import fingerprint, file_digest

DIMENSIONS = ('city_id', 'house_id', 'wall_id', 'attic_r_ip', 'orientation_deg')


def supported_values(catalog, mappings):
    cities = list(mappings.get('weather_bindings', {mappings['active_city']: mappings['weather_binding']}))
    return {'city_id': sorted(cities), 'house_id': sorted(mappings.get('house_properties', {mappings['active_house']: {}})),
            'wall_id': sorted(mappings['reviewed_wall_hashes'], key=lambda w:int(w[1:])),
            'attic_r_ip': sorted({r for city in cities for r in catalog['locations'][city]['value']['attic_levels_r_ip'] if r in mappings['active_attic_levels']}),
            'orientation_deg': [0,45,90,135]}


def default_design(catalog, mappings):
    return {'schema_version': 1, 'parameters': supported_values(catalog, mappings), 'fixed': {},
            'sampling': 'full_factorial', 'seed': 42, 'constraints': {'exclude': []}}


def physical_identity(model, weather_hash):
    """Ignore material/construction labels; retain all physical and operational fields."""
    model = deepcopy(model)
    if symmetric_180(model):
        for building in model['Building'].values():
            building['north_axis'] = float(building['north_axis']) % 180
    material_ids = {}
    for kind in ('Material', 'Material:NoMass', 'WindowMaterial:SimpleGlazingSystem'):
        if kind in model:
            material_ids.update({name: fingerprint([kind, props]) for name,props in model[kind].items()})
            model[kind] = {material_ids[name]:props for name,props in model[kind].items()}
    construction_ids = {}
    constructions = {}
    for name, fields in model['Construction'].items():
        props = {key: material_ids[value] for key,value in fields.items()}
        construction_ids[name] = fingerprint(props)
        constructions[construction_ids[name]] = props
    model['Construction'] = constructions
    for kind in ('BuildingSurface:Detailed','FenestrationSurface:Detailed'):
        for obj in model.get(kind,{}).values():
            obj['construction_name'] = construction_ids[obj['construction_name']]
    return fingerprint({'model':model,'weather_sha256':weather_hash})


@dataclass
class ExperimentDesign:
    cases: list[ResidentialInput]
    available: int
    excluded: int
    duplicates: int
    spec: dict
    counts: dict
    identity: str

    def summary(self):
        return {'available_configurations':self.available, 'requested_simulations':len(self.cases),
                'excluded_combinations':self.excluded,'equivalent_combinations_removed':self.duplicates,
                'selected_dimension_counts':self.counts,'design_identity':self.identity,
                'sampling':self.spec.get('sampling','full_factorial'),'seed':self.spec.get('seed',42)}


def generate_design(spec, catalog, mappings, assumptions, installation, weather_by_city):
    if not isinstance(spec, dict):
        raise ConfigurationError('Experiment configuration must be a JSON object')
    unknown = set(spec)-{'schema_version','parameters','fixed','sampling','count','seed','constraints','weather_files'}
    if unknown:
        raise ConfigurationError(f'Unsupported experiment fields: {sorted(unknown)}')
    if spec.get('schema_version',1)!=1:
        raise ConfigurationError('Unsupported experiment schema_version')
    supported=supported_values(catalog,mappings)
    parameters,fixed=spec.get('parameters',{}),spec.get('fixed',{})
    if not isinstance(parameters,dict) or not isinstance(fixed,dict):
        raise ConfigurationError('parameters and fixed must be objects')
    invalid=(set(parameters)|set(fixed))-set(DIMENSIONS)
    if invalid:
        raise ConfigurationError(f'Unsupported/catalog-only simulation dimensions: {sorted(invalid)}')
    if set(parameters)&set(fixed):
        raise ConfigurationError('A parameter cannot be both fixed and varying')
    defaults={'city_id':mappings['active_city'],'house_id':mappings['active_house'],'wall_id':'W3','attic_r_ip':49,'orientation_deg':0}
    selections={}
    for key in DIMENSIONS:
        values=parameters.get(key,[fixed.get(key,defaults[key])])
        if not isinstance(values,list) or not values:
            raise ConfigurationError(f'{key}: select a nonempty list or one fixed value')
        allowed=list(range(0,360,45)) if key=='orientation_deg' else supported[key]
        if any(isinstance(v,bool) or not isinstance(v,(str,int,float)) or v not in allowed for v in values):
            raise ConfigurationError(f'{key}: unsupported value. Implemented values: {allowed}')
        selections[key]=sorted(set(values))
    constraints=spec.get('constraints',{})
    if not isinstance(constraints,dict) or set(constraints)-{'exclude'} or not isinstance(constraints.get('exclude',[]),list):
        raise ConfigurationError('constraints supports only {"exclude": [{dimension: value, ...}]}')
    exclusions=constraints.get('exclude',[])
    for exclusion in exclusions:
        if not isinstance(exclusion,dict) or not exclusion or set(exclusion)-set(DIMENSIONS):
            raise ConfigurationError('Each exclusion must match known physical dimensions')
        for key,val in exclusion.items():
            if isinstance(val,bool) or val not in (list(range(0,360,45)) if key=='orientation_deg' else supported[key]):
                raise ConfigurationError(f'Unsupported exclusion value for {key}: {val}')
    strategy=spec.get('sampling','full_factorial')
    if strategy not in ('full_factorial','random','balanced'):
        raise ConfigurationError('Sampling must be full_factorial, random or balanced')
    seed=spec.get('seed',42)
    if isinstance(seed,bool) or not isinstance(seed,int):
        raise ConfigurationError('seed must be an integer')
    count=spec.get('count')
    if count is not None and (isinstance(count,bool) or not isinstance(count,int) or count<1):
        raise ConfigurationError('count must be a positive integer')
    if strategy=='full_factorial' and count is not None:
        raise ConfigurationError('Full factorial has no count; select random or balanced for a fixed count')
    if strategy!='full_factorial' and count is None:
        raise ConfigurationError('Fixed-count sampling requires count')
    weather_hashes={}
    unique={};excluded=duplicates=0
    for values in product(*(selections[key] for key in DIMENSIONS)):
        fields=dict(zip(DIMENSIONS,values));city=fields['city_id'];wall=fields['wall_id']
        if (city not in catalog['wall_assemblies'][wall]['value']['eligible_cities'] or
            fields['attic_r_ip'] not in catalog['locations'][city]['value']['attic_levels_r_ip'] or
            any(all(fields[k]==v for k,v in ex.items()) for ex in exclusions)):
            excluded+=1
            continue
        if city not in weather_by_city:
            raise ConfigurationError(f'{city}: no matching weather file; no substitute permitted')
        path=Path(weather_by_city[city]).resolve()
        if path not in weather_hashes: weather_hashes[path]=file_digest(path)
        case=ResidentialInput(weather_file=path,**fields)
        model=generate_residential_model(case,catalog,mappings,assumptions,installation.version)
        if symmetric_180(model) and case.orientation_deg>=180:
            case=replace(case,orientation_deg=case.orientation_deg%180)
        if any(all(getattr(case,k)==v for k,v in ex.items()) for ex in exclusions):
            excluded+=1
            continue
        identity=physical_identity(model,weather_hashes[path])
        if identity in unique:
            duplicates+=1
        else:
            unique[identity]=case
    pool=[unique[key] for key in sorted(unique)]
    available=len(pool)
    if not available:
        raise ConfigurationError('No valid configurations remain after city eligibility and constraints')
    if count is not None and count>available:
        raise ConfigurationError(f'Requested {count} unique simulations, but only {available} valid, symmetry-distinct configurations exist. Selected dimensions: '+', '.join(f'{k}={len(v)}' for k,v in selections.items())+'. Expand supported selections; catalog-only fields and duplicate rotations cannot increase this count.')
    rng=random.Random(seed)
    if strategy=='random':
        cases=rng.sample(pool,count)
    elif strategy=='balanced':
        # Greedy marginal balancing with deterministic randomized tie order.
        # Constraints can prevent exact balance; report achieved counts explicitly.
        candidates=list(pool);rng.shuffle(candidates);cases=[]
        counts={k:Counter() for k in DIMENSIONS}
        levels={k:len({getattr(c,k) for c in pool}) for k in DIMENSIONS}
        for _ in range(count):
            best=min(range(len(candidates)),key=lambda i:sum(levels[k]*counts[k][getattr(candidates[i],k)] for k in DIMENSIONS))
            case=candidates.pop(best);cases.append(case)
            for k in DIMENSIONS:counts[k][getattr(case,k)]+=1
    else:
        cases=pool
    achieved={k:dict(Counter(str(getattr(c,k)) for c in cases)) for k in DIMENSIONS}
    design_id=fingerprint(sorted(physical_identity(generate_residential_model(c,catalog,mappings,assumptions,installation.version),weather_hashes[c.weather_file]) for c in cases))
    return ExperimentDesign(cases,available,excluded,duplicates,deepcopy(spec),achieved,design_id)


def design_from_spec(spec, catalog, mappings, assumptions, installation):
    from thermal_home_modeling.experiments.residential import resolve_residential_weather
    if not isinstance(spec, dict) or not isinstance(spec.get('parameters',{}),dict) or not isinstance(spec.get('fixed',{}),dict):
        raise ConfigurationError('Experiment parameters and fixed values must be JSON objects')
    cities=spec.get('parameters',{}).get('city_id',[spec.get('fixed',{}).get('city_id',mappings['active_city'])])
    if not isinstance(cities,list) or not cities or any(not isinstance(c,str) for c in cities):
        raise ConfigurationError('city_id requires a nonempty list of supported city IDs')
    explicit=spec.get('weather_files',{})
    if not isinstance(explicit,dict) or set(explicit)-set(supported_values(catalog,mappings)['city_id']):
        raise ConfigurationError('weather_files must map implemented city IDs to EPW paths')
    if any(not isinstance(v,str) or not v for v in explicit.values()):
        raise ConfigurationError('weather_files values must be nonempty path strings')
    weather={city:Path(explicit[city]) if city in explicit else resolve_residential_weather(installation,mappings,city) for city in cities}
    return generate_design(spec,catalog,mappings,assumptions,installation,weather)
