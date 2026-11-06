import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
import json
import logging
import math
import os
import re
import requests
import sys
import threading
import yaml
from dotenv import load_dotenv
import gitlab

load_dotenv()

requests.packages.urllib3.disable_warnings()

logging.basicConfig(filename='lvm_info.log', filemode='w', encoding='utf-8', level=logging.WARNING)

PROJECT_ID = 1247
BRANCH = "production"

DEFAULT_SIZE_G = 50         # LV without size, or with "extents": "100%FREE"
MB_MARGIN_G = 5             # extra margin for sizes given in M/MB
LVM_RESERVED_G = 5          # per disk, reserved for lvm metadata/header
IGNORED_VGS = {'VGsystem'}
IGNORED_PV_PREFIX = '/dev/sda'

SIZE_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([MGT])B?\s*$', re.IGNORECASE)

# cache of lvm::volume_groups per hiera file, shared by all threads
# key: (pad, naam), value: volume groups dict, or None when the file has no lvm info
cache_map = {}
cache_lock = threading.Lock()


def tijdstip():
    return datetime.today().strftime('%d-%m-%Y %H:%M:%S')


def get_groups():
    base_url = os.getenv('GROUP_CHILDREN_URL')
    root_parent = os.getenv('ROOT_PARENT')
    header = {'X-Authentication': os.getenv('PUPPET_TOKEN')}

    groepresult = []
    try:
        resp = requests.get(f"{base_url}/{root_parent}", headers=header, verify=False)
        groepen = resp.json()
        for groep in groepen[0]['children']:
            if groep['name'] == 'ALL':
                continue
            if groep['immediate_child_count'] > 0:
                for subgroep in groep['children']:
                    groepresult.append({'id': subgroep['id'], 'name': subgroep['name'], 'variables': subgroep['variables']})
            else:
                groepresult.append({'id': groep['id'], 'name': groep['name'], 'variables': groep['variables']})
    except requests.exceptions.RequestException as errex:
        logging.warning(errex)
        return []

    return groepresult


def lvm_info(project, pad, naam, groep):
    """Return lvm::volume_groups from {pad}/{naam}.yaml, using the shared cache."""
    sleutel = (pad, naam)
    with cache_lock:
        if sleutel in cache_map:
            return cache_map[sleutel]

    try:
        file = project.files.get(file_path=f"{pad}/{naam}.yaml", ref=BRANCH)
        file_content = base64.b64decode(file.content).decode("utf-8")
        yfile = yaml.safe_load(file_content) or {}
        diskinfo = yfile.get('lvm::volume_groups') or None
    except gitlab.exceptions.GitlabGetError as e:
        logging.warning(f"{tijdstip()},{naam},{pad}/{naam}.yaml,{groep['name']},{groep['id']}, {e}")
        diskinfo = None
        if e.response_code != 404:
            return diskinfo     # don't cache errors other than 'file not found'
    except Exception as e:
        logging.warning(f"{tijdstip()},{naam},{pad}/{naam}.yaml,{groep['name']},{groep['id']}, {e}")
        return None

    with cache_lock:
        cache_map[sleutel] = diskinfo

    return diskinfo


def size_in_g(lv_name, lv_conf):
    """Return the size of one logical volume in G, following the rules in mail.txt."""
    size = lv_conf.get('size')
    if size is None:
        # "extents": "100%FREE" and "no size given" both count as 50G
        return DEFAULT_SIZE_G

    match = SIZE_RE.match(str(size))
    if not match:
        logging.warning(f"{tijdstip()},{lv_name}: unknown size '{size}', using {DEFAULT_SIZE_G}G")
        return DEFAULT_SIZE_G

    value, unit = float(match.group(1)), match.group(2).upper()
    if unit == 'M':
        return math.ceil(value / 1000) + MB_MARGIN_G
    if unit == 'T':
        return value * 1000
    return value


def merged_volume_groups(tempresult):
    """Deep merge all VG definitions of the profiles and the role into one dict per VG.

    Later sources override earlier ones, the role overrides the profiles. This way
    a VG whose physical_volumes are only defined in the role is still linked to its
    disk, and a LV that is defined in both a profile and the role is counted once.
    """
    bronnen = [vgs for profiel in tempresult['profielen'] for vgs in profiel.values()]
    bronnen += list(tempresult['rol'].values())

    vgs = {}
    for bron in bronnen:
        if not isinstance(bron, dict):
            continue
        for vg_name, vg_conf in bron.items():
            if vg_name in IGNORED_VGS or not isinstance(vg_conf, dict):
                continue
            vg = vgs.setdefault(vg_name, {'physical_volumes': [], 'logical_volumes': {}})
            for pv in vg_conf.get('physical_volumes') or []:  # list or dict, both iterate PV names
                if pv not in vg['physical_volumes']:
                    vg['physical_volumes'].append(pv)
            for lv_name, lv_conf in (vg_conf.get('logical_volumes') or {}).items():
                vg['logical_volumes'].setdefault(lv_name, {}).update(lv_conf or {})

    return vgs


def required_disks(tempresult):
    disks = {}
    for vg_name, vg in merged_volume_groups(tempresult).items():
        pvs = [pv for pv in vg['physical_volumes'] if not pv.startswith(IGNORED_PV_PREFIX)]
        if not pvs:
            if not vg['physical_volumes']:
                logging.warning(f"{tijdstip()},{tempresult['name']},{tempresult['id']}: {vg_name} has no physical_volumes, skipped")
            continue
        if len(pvs) > 1:
            logging.warning(f"{tijdstip()},{tempresult['name']},{tempresult['id']}: {vg_name} has multiple physical_volumes {pvs}, size assigned to {pvs[0]}")

        totaal = sum(
            size_in_g(lv_name, lv_conf)
            for lv_name, lv_conf in vg['logical_volumes'].items()
            if lv_conf.get('ensure') != 'absent'
        )
        disks[pvs[0]] = disks.get(pvs[0], 0) + totaal

    # sorted on length first, so /dev/sdz comes before /dev/sdaa
    return {
        pv: [str(math.ceil(totaal + LVM_RESERVED_G))]
        for pv, totaal in sorted(disks.items(), key=lambda item: (len(item[0]), item[0]))
    }


def hiera_info(groep, project):

    tempresult = {}
    tempresult['id'] = groep['id']
    tempresult['name'] = groep['name']
    tempresult['profielen'] = []
    tempresult['rol'] = {}
    profielen = groep['variables'].get('profiles', '')
    rol = groep['variables'].get('role', '')
    huidig = groep['variables'].get('required_disks', {})

    for profiel in profielen or []:
        diskinfo = lvm_info(project, "profiles", profiel, groep)
        if diskinfo:
            tempresult['profielen'].append({profiel: diskinfo})

    if rol:
        diskinfo = lvm_info(project, "role", rol, groep)
        if diskinfo:
            tempresult['rol'] = {rol: diskinfo}

    tempresult['required_disks'] = required_disks(tempresult)

    # nothing to change in puppet
    if tempresult['required_disks'] == (huidig or {}):
        return {}

    return tempresult


def connect_gl(pool_size):
    gl_url = os.getenv("GL_URL")
    gl_token = os.getenv("GL_TOKEN")

    # one connection per thread, the requests default of 10 is too small for MAX_THREAD
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=pool_size)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    gl = gitlab.Gitlab(url=gl_url, private_token=gl_token, session=session)

    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError as e:
        logging.error(f"{tijdstip()},Token: {e}")
        sys.exit(1)

    return gl


def main():

    groepen = get_groups()

    if len(groepen) > 0:
        MAX_THREAD = min(32, os.cpu_count() + 4)

        gl = connect_gl(MAX_THREAD)
        project = gl.projects.get(PROJECT_ID, lazy=True)
        fn = partial(hiera_info, project=project)

        with ThreadPoolExecutor(max_workers=MAX_THREAD) as executor:
            results = executor.map(fn, groepen)

        lst = [d for d in results if d]
        with open('nodegroup_lvm.json', 'w') as f:
            f.write(json.dumps(lst, indent=2))

if __name__ == '__main__':
    main()
