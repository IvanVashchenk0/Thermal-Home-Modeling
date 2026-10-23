"""Implementation annotations remain separate from immutable workbook data."""
from copy import deepcopy
import json
import re

from thermal_home_modeling.catalogs.workbook import CATALOG_ROOT
from thermal_home_modeling.utilities.files import fingerprint, file_digest


def effective_registry(catalog, mappings, assumptions=None):
    records = deepcopy(catalog['parameters'])
    evidence_path = CATALOG_ROOT / 'residential_validation.json'
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
    verified = (assumptions is not None and evidence.get('catalog_revision') == catalog['workbook_sha256']
                and evidence.get('mappings_hash') == fingerprint(mappings)
                and evidence.get('assumptions_hash') == fingerprint(assumptions)
                and evidence.get('generator_sha256') == file_digest(CATALOG_ROOT.parent / 'thermal_home_modeling/energyplus/residential_model.py'))
    cities = mappings.get('weather_bindings', {mappings['active_city']: {}})
    active = {*cities, *mappings.get('house_properties', {mappings['active_house']: {}}), 'orientation',
              *('attic_'+city for city in cities), *mappings['reviewed_wall_hashes']}
    for p in records:
        def unresolved(value):
            if isinstance(value, dict):
                return [item for v in value.values() for item in unresolved(v)]
            if isinstance(value, list):
                return [item for v in value for item in unresolved(v)]
            return [value] if isinstance(value, str) and re.search(r'\b(TBD|pending|to verify|confirm|placeholder)\b', value, re.I) else []
        p['unresolved_decisions'] = list(dict.fromkeys(unresolved(p['value']) + unresolved(p['notes'])))
        if p['parameter_id'] not in active:
            continue
        p.update(implementation_status='Model mapping implemented', changes_model=True, batch_eligible=True)
        pid=p['parameter_id']
        if verified and pid != 'orientation':
            p['implementation_status'] = 'Simulation verified'
            p['verification_scope'] = 'Eight DC/1S/W3-W9/R49-R80/0-90 pilot cases; see residential_validation.json. Not validated for research.'
        if verified and pid == 'orientation':
            p['simulation_verified_values'] = [0, 90]
        if pid in mappings['reviewed_wall_hashes']:
            p['units']={'thickness':'m','conductivity':'W/(m K)','density':'kg/m3','specific_heat':'J/(kg K)','thermal_resistance':'m2 K/W'}
            if fingerprint(p['value']['mapping']) != mappings['reviewed_wall_hashes'][pid]:
                p.update(implementation_status='Catalog only', changes_model=False, batch_eligible=False)
                p['unresolved_decisions'].append('Physical definition changed; mapping review required')
        if pid.startswith('attic_') and p['category']=='attic_insulation':
            p['allowed_values']=[r for r in p['allowed_values'] if r in mappings['active_attic_levels']]
        p['constraints'].append('Residential V0 only; model generation validates weather, eligibility and reviewed physical properties')
        p['notes'] += ' Software verification does not establish research validation.'
    return records
