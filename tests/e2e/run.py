"""Windows E2E orchestrator. Installed PostgreSQL, Chrome and Node only.

Run with backend/.venv/Scripts/python.exe -B -X utf8 tests/e2e/run.py --temp-root D:/INF4173-SJ-tests
No cleanup deletes files. Owned services are stopped in finally; artifacts remain.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
import uuid

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / 'backend/.venv/Scripts/python.exe'
PG = Path('C:/Program Files/PostgreSQL/18/bin')
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
parser = argparse.ArgumentParser()
parser.add_argument('--temp-root', required=True, type=Path)
parser.add_argument('--skip-backend-tests', action='store_true', help='For harness debugging only, after an unchanged backend already passed.')
parser.add_argument('--frontend-only', action='store_true', help='Recheck frontend test infrastructure without restarting PostgreSQL or Chrome.')
args = parser.parse_args()
temp_root = args.temp_root.resolve()
if temp_root.drive.upper() != 'D:':
    raise SystemExit('This run explicitly requires the user-approved D: temporary storage.')
if shutil.disk_usage(temp_root.anchor).free < 1024**3:
    raise SystemExit('At least 1 GiB free on D: required.')
for port in (() if args.frontend_only else (55484, 18000, 15173, 19222)):
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1',port)) == 0:
            raise SystemExit(f'Required isolated port {port} is occupied; nothing stopped or changed.')
temp = temp_root / ('lot6-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
temp.mkdir(parents=True)
env = os.environ.copy()
env.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
           SYNC_TEST_DB_HOST='127.0.0.1', SYNC_TEST_DB_PORT='55484',
           SYNC_TEST_DB_USER='sync_test_e2e', SYNC_TEST_DB_NAME='suivi_sync_test_e2e_lot6', E2E_TEMP=str(temp))
env['PATH']=str(PG)+os.pathsep+env.get('PATH','')
children, handles = [], []
cluster_started = False
report = {'temporary_directory':str(temp),'browser':'installed Chrome headless','providers':'simulated','main_unchanged':None,'checks':{}}
plan = ROOT/'docs/plan_de_travail_INF4173.pdf'
plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest() if plan.is_file() else None
report['private_plan_present'] = plan_hash is not None

def run(label, cmd, cwd=ROOT, custom_env=None):
    with (temp / (label+'.log')).open('w',encoding='utf-8') as log:
        r = subprocess.run([str(x) for x in cmd],cwd=cwd,env=custom_env or env,stdout=log,stderr=subprocess.STDOUT,creationflags=FLAGS)
    if r.returncode:
        raise RuntimeError(f'{label} failed; see isolated log {temp / (label+".log")}')
    report['checks'][label]=True
    print('OK '+label,flush=True)

def spawn(label, cmd, cwd=ROOT):
    log=(temp/(label+'.log')).open('w',encoding='utf-8');handles.append(log)
    child=subprocess.Popen([str(x) for x in cmd],cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=FLAGS)
    children.append(child)
    return child

def wait_url(url):
    until=time.monotonic()+30
    while time.monotonic()<until:
        try:
            with urlopen(url,timeout=1) as r:
                if r.status==200:return
        except Exception:
            time.sleep(.2)
    raise RuntimeError('Isolated server readiness timeout')

def primary_snapshot():
    # Read-only SQL transaction, hashes only: neither rows nor credentials leave process.
    script = '''import os,json,hashlib,django
os.environ['DJANGO_SETTINGS_MODULE']='config.settings'
django.setup()
from django.db import connection
with connection.cursor() as c:
 c.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
 result={}
 for table in connection.introspection.table_names():
  c.execute('SELECT to_jsonb(t)::text FROM '+connection.ops.quote_name(table)+' t')
  rows=sorted(hashlib.sha256(r[0].encode()).hexdigest() for r in c.fetchall())
  result[table]={'count':len(rows),'hash':hashlib.sha256(''.join(rows).encode()).hexdigest()}
 c.execute('ROLLBACK')
print(json.dumps(result,sort_keys=True))'''
    r=subprocess.run([str(PYTHON),'-B','-X','utf8','-c',script],cwd=ROOT/'backend',env=env,capture_output=True,text=True,creationflags=FLAGS)
    if r.returncode:raise RuntimeError('Read-only primary fingerprint failed; no details exposed.')
    return json.loads(r.stdout)

def frontend_checks(node):
    frontend = ROOT/'frontend'
    tsc = frontend/'node_modules/typescript/bin/tsc'
    run('frontend-tests',[node,'--test',*sorted((frontend/'tests').glob('*.test.mjs'))],frontend)
    for project in ('app','node'):
        run('typescript-'+project,[node,tsc,'--project',f'tsconfig.{project}.json','--incremental',
            '--tsBuildInfoFile',temp/f'typescript-{project}.tsbuildinfo'],frontend)
    run('typescript-e2e',[node,tsc,'--ignoreConfig','--noEmit','--skipLibCheck','--target','ES2023',
        '--module','nodenext','--moduleResolution','nodenext','--types','node','vite.e2e.config.ts'],frontend)
    run('lint',[node,frontend/'node_modules/oxlint/bin/oxlint'],frontend)
    run('build',[node,frontend/'node_modules/vite/bin/vite.js','build','--outDir',temp/'frontend-dist'],frontend)

if args.frontend_only:
    report['scope']='frontend-only'
    try:
        frontend_checks(shutil.which('node.exe') or shutil.which('node'))
    except Exception as error:
        report['error']=str(error)
        print('ERROR '+str(error),flush=True)
    (temp/'run-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('Artifacts: '+str(temp),flush=True)
    raise SystemExit(1 if report.get('error') else 0)

before = primary_snapshot()
(temp/'primary-before.json').write_text(json.dumps(before),encoding='utf-8')
try:
    run('initdb',[PG/'initdb.exe','-D',temp/'postgres','--auth=trust','--encoding=UTF8','--locale=C','--wal-segsize=1','-U','sync_test_e2e'])
    run('postgres-start',[PG/'pg_ctl.exe','-D',temp/'postgres','-l',temp/'postgres.log','-o',
        '-p 55484 -h 127.0.0.1 -c shared_buffers=16MB -c max_connections=30 -c max_wal_size=16MB -c min_wal_size=2MB','-w','start'])
    cluster_started=True
    run('create-isolated-db',[PG/'createdb.exe','-h','127.0.0.1','-p','55484','-U','sync_test_e2e','suivi_sync_test_e2e_lot6'])
    if not args.skip_backend_tests:
        run('backend-tests',[PYTHON,'-B','-X','utf8','manage.py','test','--settings=config.test_postgres_settings','--noinput'],ROOT/'backend')
    run('isolated-migrate',[PYTHON,'-B','-X','utf8','manage.py','migrate','--settings=config.e2e_settings','--noinput'],ROOT/'backend')
    run('django-check',[PYTHON,'-B','-X','utf8','manage.py','check','--settings=config.e2e_settings'],ROOT/'backend')
    run('migration-drift',[PYTHON,'-B','-X','utf8','manage.py','makemigrations','--check','--dry-run','--settings=config.e2e_settings'],ROOT/'backend')
    run('production-security',[PYTHON,'-B','-X','utf8','tests/security/check_deploy.py'])
    run('fixtures',[PYTHON,'-B','-X','utf8','tests/e2e/backend_control.py','seed'])
    spawn('django',[PYTHON,'-B','-X','utf8','tests/e2e/backend_control.py','serve'])
    node=shutil.which('node.exe') or shutil.which('node')
    if not node:raise RuntimeError('Installed Node.js required.')
    spawn('vite',[node,ROOT/'frontend/node_modules/vite/bin/vite.js','--config','vite.e2e.config.ts'],ROOT/'frontend')
    chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    if not chrome.exists():raise RuntimeError('Installed Chrome not found; no download attempted.')
    spawn('chrome',[chrome,'--headless=new','--remote-debugging-port=19222','--remote-debugging-address=127.0.0.1',
        '--user-data-dir='+str(temp/'chrome-profile'),'--disk-cache-dir='+str(temp/'chrome-cache'),
        '--crash-dumps-dir='+str(temp/'chrome-crashes'),'--no-first-run','--no-default-browser-check',
        '--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',
        '--disable-default-apps','--metrics-recording-only','--password-store=basic','about:blank'])
    wait_url('http://127.0.0.1:15173');wait_url('http://127.0.0.1:18000/api/auth/csrf/');wait_url('http://127.0.0.1:19222/json/version')
    with urlopen('http://127.0.0.1:19222/json/version',timeout=2) as response:
        version=json.load(response)
    report['browser_version']=version['Browser']
    run('browser-tests',[node,'tests/e2e/browser.mjs'])
    frontend_checks(node)
except Exception as error:
    report['error']=str(error);print('ERROR '+str(error),flush=True)
finally:
    for child in reversed(children):
        if child.poll() is None:
            # Windows taskkill targets only the owned isolated process tree, never user servers.
            subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,creationflags=FLAGS)
    for handle in handles:handle.close()
    if cluster_started:
        run('postgres-stop',[PG/'pg_ctl.exe','-D',temp/'postgres','-m','fast','-w','stop'])
    after=primary_snapshot()
    (temp/'primary-after.json').write_text(json.dumps(after),encoding='utf-8')
    report['main_unchanged']=before==after
    report['plan_unchanged']=(plan.is_file() and hashlib.sha256(plan.read_bytes()).hexdigest()==plan_hash) if plan_hash else None
    if before!=after:report['error']='Primary database fingerprint changed during test; investigate concurrent activity.'
    (temp/'run-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('Primary unchanged: '+str(before==after),flush=True)
    print('Artifacts: '+str(temp),flush=True)
if report.get('error'):raise SystemExit(1)
