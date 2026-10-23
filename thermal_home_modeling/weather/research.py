"""Reproducible retrieval of the reviewed research-city TMY3 archives."""
from pathlib import Path
import hashlib
from io import BytesIO
from urllib.request import urlopen
from zipfile import ZipFile

from thermal_home_modeling.catalogs.workbook import CATALOG_ROOT
from thermal_home_modeling.energyplus.residential_model import load_v0_configuration
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.utilities.files import atomic_destination, file_digest
from thermal_home_modeling.weather.discovery import read_epw_location


def fetch_research_weather():
    mappings,_=load_v0_configuration()
    root=CATALOG_ROOT.parent/'data/weather/research'
    root.mkdir(parents=True,exist_ok=True)
    files=[]
    for city,binding in mappings['weather_bindings'].items():
        if 'source_url' not in binding:
            continue  # DC uses the separately installed, explicitly bound Dulles EPW.
        target=root/binding['file_name']
        if target.exists():
            if file_digest(target)!=binding['epw_sha256']:
                raise ConfigurationError(f'{target}: differs from reviewed weather hash; retained unchanged for review')
            files.append(str(target));continue
        with urlopen(binding['source_url'],timeout=60) as response:
            payload=response.read()
        if hashlib.sha256(payload).hexdigest()!=binding['archive_sha256']:
            raise ConfigurationError(f'{city}: upstream archive changed; review before using it')
        with ZipFile(BytesIO(payload)) as archive:
            names=[n for n in archive.namelist() if Path(n).name==target.name]
            if len(names)!=1: raise ConfigurationError(f'{city}: expected one matching EPW')
            data=archive.read(names[0])
        if hashlib.sha256(data).hexdigest()!=binding['epw_sha256']:
            raise ConfigurationError(f'{city}: EPW checksum mismatch')
        with atomic_destination(target) as temporary: temporary.write_bytes(data)
        read_epw_location(target)
        files.append(str(target))
    return files
