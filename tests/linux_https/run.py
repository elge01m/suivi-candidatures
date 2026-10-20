"""Manual CI integration, Linux VM only. Never connects to the application DB.

Only a small allowlisted result is printed. No raw process output, HTTP payload,
cookie, key, archive or journal is uploaded. Run cleanup in an always() CI step.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import site
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = Path('/srv/inf4173-linux-test')
RELEASE = BASE / 'current'
STATE = Path('/run/inf4173-linux-test')
UNITS = ('inf4173-test-web.service', 'inf4173-test-sync.service', 'inf4173-test-nginx.service')
HOST = 'candidatures.example.invalid'
HOST_LINE = '127.0.0.1 ' + HOST + ' # inf4173-linux-https-fixture\n'
ENV_FILE = STATE / 'environment.json'


def command(args, *, env=None, cwd=None, capture=False, timeout=180):
    result = subprocess.run([str(x) for x in args], env=env, cwd=cwd,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Subprocess failed: ' + Path(str(args[0])).name)
    return result.stdout if capture else None


def private_write(path, data):
    path.write_text(data, encoding='utf-8')
    path.chmod(0o600)


def guard():
    if not (sys.platform == 'linux' and os.geteuid() == 0
            and os.environ.get('GITHUB_ACTIONS') == 'true'):
        raise RuntimeError('Requires an explicitly authorized ephemeral GitHub Ubuntu runner')
    if not Path('/run/systemd/system').is_dir():
        raise RuntimeError('systemd is required; no fallback pretending to validate services')


def environment():
    data = json.loads(ENV_FILE.read_text())
    # Nothing is inherited from developer dotenv, cloud identities or runner secrets.
    return data


def fixture(action):
    command([RELEASE/'backend/.venv/bin/python', '-B', RELEASE/'tests/linux_https/entry.py', action],
            env=environment(), cwd=RELEASE/'backend')


def prepare():
    if BASE.exists() or STATE.exists():
        raise RuntimeError('Existing fixture directory refused')
    for port in (80,443,8001):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',port))
    STATE.mkdir(mode=0o700)
    command(['useradd', '--system', '--no-create-home', '--shell', '/usr/sbin/nologin', 'inf4173test'])
    RELEASE.mkdir(parents=True)
    # Copy only Git-listed code. No .env, .git, venv, archive, journal or local helper.
    paths=command(['git','ls-files','-z'],cwd=ROOT,capture=True).decode().split('\0')
    for name in filter(None, paths):
        source=ROOT/name
        if source.is_symlink() or Path(name).is_absolute() or '..' in Path(name).parts:
            raise RuntimeError('Unsafe release path')
        dest=RELEASE/name;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,dest);dest.chmod(0o644)
    shutil.copytree(ROOT/'frontend/dist',RELEASE/'frontend/dist')
    command([sys.executable,'-m','venv',RELEASE/'backend/.venv'])
    python=RELEASE/'backend/.venv/bin/python'
    command([python,'-m','pip','install','-r',RELEASE/'backend/requirements-production.txt'],timeout=300)
    site.addsitedir(str(next((RELEASE/'backend/.venv/lib').glob('python*/site-packages'))))
    env={'PATH':'/usr/lib/postgresql/18/bin:/usr/local/bin:/usr/bin:/bin',
         'HOME':str(STATE),'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1',
         'DJANGO_SETTINGS_MODULE':'config.production_settings','DJANGO_LOAD_DOTENV':'0',
         'LINUX_HTTPS_FIXTURE':'1','GITHUB_ACTIONS':'true',
         'DB_HOST':'127.0.0.1','DB_PORT':'55486','DB_NAME':'suivi_sync_test_linux_https',
         'DB_USER':'sync_test_linux_app','DB_PASSWORD':'public-fiction-linux-app-password',
         'DB_SSLMODE':'disable','APP_ORIGIN':'https://'+HOST,
         'DJANGO_ALLOWED_HOSTS':HOST,'DJANGO_CSRF_TRUSTED_ORIGINS':'https://'+HOST,
         'DJANGO_SECRET_KEY':'Public-fiction-linux-check-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ',
         'GOOGLE_CLIENT_ID':'fiction','GOOGLE_CLIENT_SECRET':'fiction-never-used',
         'GEMINI_API_KEY':'fiction-never-used','GOOGLE_REDIRECT_URI':'https://'+HOST+'/api/gmail/callback/',
         'OAUTH_SUCCESS_REDIRECT_URL':'https://'+HOST+'/',
         'DJANGO_STATIC_ROOT':str(RELEASE/'backend/staticfiles')}
    from cryptography.fernet import Fernet
    # Dedicated disposable keys, never printed or exported as artifacts.
    for key in ('GOOGLE_TOKEN_ENCRYPTION_KEY','GMAIL_MESSAGE_ENCRYPTION_KEY'):
        env[key]=Fernet.generate_key().decode()
    private_write(ENV_FILE,json.dumps(env))
    # systemd parses a separate EnvironmentFile; all generated values are simple literals.
    private_write(STATE/'backend.env',''.join(k+'="'+v+'"\n' for k,v in env.items()))
    command(['chown','root:inf4173test',STATE,STATE/'backend.env'])
    STATE.chmod(0o750);(STATE/'backend.env').chmod(0o640)
    import psycopg
    with psycopg.connect(host='127.0.0.1',port=55486,user='sync_test_linux_admin',
                         password='public-ephemeral-linux-password',dbname='postgres',autocommit=True) as conn:
        conn.execute("CREATE ROLE sync_test_linux_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'public-fiction-linux-app-password'")
        conn.execute('CREATE DATABASE suivi_sync_test_linux_https OWNER sync_test_linux_app')
    fixture('migrate');fixture('seed')
    # Every public directory is traversable, private credentials remain restricted.
    for folder in RELEASE.rglob('*'):
        if folder.is_dir():folder.chmod(0o755)
    hosts=Path('/etc/hosts')
    if HOST in hosts.read_text():raise RuntimeError('Existing fixture hostname refused')
    with hosts.open('a') as out:out.write('\n'+HOST_LINE)
    # Local CA + server certificate, no ACME/DNS/public certificate service.
    openssl=['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1']
    command(openssl+['-keyout',STATE/'ca.key','-out',STATE/'ca.crt','-subj','/CN=INF4173 fictional local CA',
                     '-addext','basicConstraints=critical,CA:TRUE',
                     '-addext','keyUsage=critical,keyCertSign,cRLSign'])
    command(['openssl','req','-newkey','rsa:2048','-nodes','-keyout',STATE/'server.key','-out',STATE/'server.csr','-subj','/CN='+HOST])
    private_write(STATE/'server.ext','subjectAltName=DNS:'+HOST+'\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\n')
    command(['openssl','x509','-req','-in',STATE/'server.csr','-CA',STATE/'ca.crt','-CAkey',STATE/'ca.key',
             '-CAcreateserial','-out',STATE/'server.crt','-days','1','-extfile',STATE/'server.ext'])
    for path in STATE.glob('*'):
        if path.is_file():path.chmod(0o600)
    (STATE/'backend.env').chmod(0o640)
    nginx_site=(ROOT/'deploy/nginx/suivi.conf').read_text().replace('/srv/suivi-candidatures',str(BASE))
    nginx_site=nginx_site.replace('listen 80','listen 127.0.0.1:80').replace('listen 443','listen 127.0.0.1:443')
    nginx_site=nginx_site.replace('/etc/letsencrypt/live/'+HOST+'/fullchain.pem',str(STATE/'server.crt'))
    nginx_site=nginx_site.replace('/etc/letsencrypt/live/'+HOST+'/privkey.pem',str(STATE/'server.key'))
    nginx_site=nginx_site.replace('/etc/nginx/snippets/suivi-proxy.conf',str(STATE/'proxy.conf'))
    nginx_site=nginx_site.replace('/var/log/nginx/suivi-access.log',str(STATE/'nginx-access.log'))
    private_write(STATE/'proxy.conf',(ROOT/'deploy/nginx/suivi-proxy.conf').read_text())
    private_write(STATE/'nginx.conf','user www-data;\npid '+str(STATE/'nginx.pid')+';\nevents {}\nhttp {\ninclude /etc/nginx/mime.types;\n'+nginx_site+'\n}\n')
    for kind in ('web','sync'):
        text=(ROOT/('deploy/systemd/suivi-'+kind+'.service')).read_text()
        text=text.replace('/srv/suivi-candidatures',str(BASE)).replace('/etc/suivi-candidatures/backend.env',str(STATE/'backend.env'))
        text=text.replace('User=suivi','User=inf4173test').replace('Group=suivi','Group=inf4173test')
        lines=text.splitlines()
        # Production hardening retained. Only the test entry/finite fake worker differs.
        lines=[('ExecStart='+str(python)+' -B '+str(RELEASE/'tests/linux_https/entry.py')+' '+('web' if kind=='web' else 'worker')) if line.startswith('ExecStart=') else line for line in lines]
        private_write(Path('/etc/systemd/system/inf4173-test-'+kind+'.service'),'\n'.join(lines)+'\n')
    private_write(Path('/etc/systemd/system/inf4173-test-nginx.service'),
        '[Unit]\nDescription=Loopback-only fictional TLS integration\n[Service]\nType=simple\nExecStart=/usr/sbin/nginx -c '+str(STATE/'nginx.conf')+' -g "daemon off;"\nExecStop=/bin/kill -QUIT $MAINPID\nUMask=0077\n')
    timer=(ROOT/'deploy/systemd/suivi-sync.timer').read_text().replace('Unit=suivi-sync.service','Unit=inf4173-test-sync.service')
    private_write(STATE/'inf4173-test-sync.timer',timer)
    command(['systemctl','daemon-reload'])
    command(['systemd-analyze','verify',*[Path('/etc/systemd/system')/u for u in UNITS],STATE/'inf4173-test-sync.timer'])
    command(['systemd-analyze','calendar','*-*-* *:*:00'])
    command(['nginx','-t','-c',STATE/'nginx.conf'])
    print('OK isolated production configuration, local TLS and unit syntax',flush=True)


def probe_http(session, url, *, headers=None, allow_redirects=True):
    """Return only a validated status or fixed error category; never response data."""
    import requests
    try:
        status=session.get(url,headers=headers,timeout=2,allow_redirects=allow_redirects).status_code
        return {'http_status':status} if type(status) is int and 100<=status<=599 else {'error':'connection_error'}
    except requests.exceptions.SSLError:
        return {'error':'tls_error'}
    except requests.exceptions.Timeout:
        return {'error':'timeout'}
    except requests.RequestException:
        return {'error':'connection_error'}


def unit_diagnostic(unit):
    """Strict property/value allowlist, not systemctl status or a journal dump."""
    allowed={
        'ActiveState':{'active','inactive','failed','activating','deactivating','reloading','maintenance'},
        'SubState':{'running','dead','failed','exited','start-pre','start','start-post',
                    'stop','stop-sigterm','stop-sigkill','stop-post','final-sigterm',
                    'final-sigkill','auto-restart','auto-restart-queued','condition','reload'},
        'Result':{'success','exit-code','signal','core-dump','timeout','watchdog',
                  'start-limit-hit','resources','protocol','oom-kill','exec-condition'},
        'ExecMainStatus':None,'NRestarts':None,
    }
    result={name:'unavailable' for name in allowed}
    if unit not in UNITS:return result
    try:
        response=subprocess.run(['systemctl','show',unit,*['--property='+name for name in allowed]],
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=2)
        if response.returncode:return result
        for line in response.stdout.decode('utf-8',errors='replace').splitlines():
            name,sep,value=line.partition('=')
            if not sep or name not in allowed:continue
            if allowed[name] is not None:
                if value in allowed[name]:result[name]=value
            elif value.isascii() and value.isdecimal() and len(value)<=10 and int(value)<=2147483647:
                result[name]=int(value)
    except (OSError,subprocess.SubprocessError):
        pass
    return result


def local_port_listening(port):
    if port not in (443,8001):raise ValueError('Diagnostic port refused')
    try:
        with socket.socket() as probe:
            probe.settimeout(1)
            return probe.connect_ex(('127.0.0.1',port))==0
    except OSError:
        return False


def readiness_diagnostic(last_probe):
    """Called only after failed readiness. No cookies, env, logs or exception text."""
    import requests
    status=last_probe.get('http_status')
    safe_probe=({'http_status':status} if type(status) is int and 100<=status<=599
                else {'error':last_probe.get('error') if last_probe.get('error') in
                      ('tls_error','connection_error','timeout') else 'connection_error'})
    with requests.Session() as direct:
        direct.trust_env=False
        backend=probe_http(direct,'http://127.0.0.1:8001/api/auth/csrf/',headers={
            'Host':HOST,'X-Forwarded-Proto':'https','X-Forwarded-For':'127.0.0.1','X-Real-IP':'127.0.0.1'},allow_redirects=False)
    report={'https_last_probe':safe_probe,
            'units':{unit:unit_diagnostic(unit) for unit in (UNITS[0],UNITS[2])},
            'listening':{str(port):local_port_listening(port) for port in (443,8001)},
            'gunicorn_http_status':backend.get('http_status')}
    print('READINESS_DIAGNOSTIC '+json.dumps(report,sort_keys=True),flush=True)


def wait_for_https(session, url):
    for _ in range(30):
        last=probe_http(session,url+'/api/auth/csrf/')
        if last.get('http_status')==200:return
        time.sleep(1)
    readiness_diagnostic(last)
    raise RuntimeError('HTTPS services did not become ready')


def test():
    import requests
    import ssl
    from entry import install_network_guard
    install_network_guard()
    checks=[]
    def ok(name):checks.append(name);print('OK '+name,flush=True)
    command(['systemctl','start','inf4173-test-web.service','inf4173-test-nginx.service'])
    session=requests.Session();session.trust_env=False;session.verify=str(STATE/'ca.crt')
    url='https://'+HOST
    wait_for_https(session,url)
    ok('actual Gunicorn and Nginx ready')
    r=session.get('http://'+HOST+'/',allow_redirects=False,timeout=5)
    assert r.status_code==308 and r.headers['Location']==url+'/'
    for protocol in (ssl.TLSVersion.TLSv1_2,ssl.TLSVersion.TLSv1_3):
        ctx=ssl.create_default_context(cafile=str(STATE/'ca.crt'));ctx.minimum_version=ctx.maximum_version=protocol
        with socket.create_connection(('127.0.0.1',443),timeout=5) as s:
            with ctx.wrap_socket(s,server_hostname=HOST):pass
    untrusted=requests.Session();untrusted.trust_env=False
    try:untrusted.get(url,timeout=5,verify=True)
    except requests.exceptions.SSLError:pass
    else:raise AssertionError('Untrusted CA accepted')
    ctx=ssl.create_default_context(cafile=str(STATE/'ca.crt'))
    try:
        with socket.create_connection(('127.0.0.1',443),timeout=5) as s:
            with ctx.wrap_socket(s,server_hostname='wrong.example.invalid'):pass
    except ssl.SSLError:pass
    else:raise AssertionError('Wrong certificate name accepted')
    ok('TLS1.2 TLS1.3, trust and name rejection, HTTP redirect')
    assert session.post(url+'/api/auth/login/',json={},timeout=5).status_code==403
    csrf=session.cookies['csrftoken']
    headers={'X-CSRFToken':csrf,'Origin':url}
    assert session.post(url+'/api/auth/login/',json={'username':'linux-fiction','password':'fiction-only-password'},headers=headers,timeout=5).status_code==200
    assert session.cookies['csrftoken']!=csrf
    assert all(c.secure for c in session.cookies if c.name in ('sessionid','csrftoken'))
    assert next(c for c in session.cookies if c.name=='sessionid').has_nonstandard_attr('HttpOnly')
    assert session.get(url+'/api/auth/me/',timeout=5).status_code==200
    headers['X-CSRFToken']=session.cookies['csrftoken']
    assert session.post(url+'/api/sync/runs/',json={},timeout=5).status_code==403
    bad=headers|{'Origin':'https://evil.example.invalid'}
    assert session.post(url+'/api/sync/runs/',json={},headers=bad,timeout=5).status_code==403
    ok('secure session, CSRF rotation and anonymous/authenticated refusal')
    page=session.get(url+'/',timeout=5);assert page.status_code==200
    import re
    asset=re.search(r'src="(/assets/[^\"]+)"',page.text);assert asset
    assert session.get(url+asset[1],timeout=5).status_code==200
    assert session.get(url+'/static/admin/css/base.css',timeout=5).status_code==200
    assert session.get(url+'/.env',timeout=5).status_code==404
    assert page.headers.get('Strict-Transport-Security')
    ok('React build, Django static files, hidden-file refusal and headers')
    command(['systemctl','restart','inf4173-test-web.service'])
    command(['systemctl','is-active','--quiet','inf4173-test-web.service'])
    assert command(['systemctl','show','-p','User','--value','inf4173-test-web.service'],capture=True).strip()==b'inf4173test'
    assert command(['systemctl','show','-p','ProtectSystem','--value','inf4173-test-web.service'],capture=True).strip()==b'strict'
    for prop in ('NoNewPrivileges','PrivateTmp','ProtectHome','PrivateDevices'):
        assert command(['systemctl','show','-p',prop,'--value','inf4173-test-web.service'],capture=True).strip()==b'yes'
    command(['systemd-analyze','security','--no-pager','inf4173-test-web.service','inf4173-test-sync.service'])
    assert (STATE/'backend.env').stat().st_mode & 0o777 == 0o640
    for _ in range(30):
        try:
            if session.get(url+'/api/auth/me/',timeout=2).status_code==200:break
        except requests.RequestException:pass
        time.sleep(1)
    else:raise RuntimeError('Restarted web service did not become ready')
    for run in range(2):
        assert session.post(url+'/api/sync/runs/',json={},headers=headers,timeout=5).status_code==202
        # Distinct service invocations persist 10 then 2 messages. No browser dependency.
        for batch in range(2):
            command(['systemctl','start','inf4173-test-sync.service'])
            fixture('verify-first' if run==0 and batch==0 else 'verify-final' if run==1 and batch==1 else 'verify-twelve')
    ok('systemd worker resume, 12 imported, 12 duplicates, encrypted messages, AI disabled')
    assert session.post(url+'/api/auth/logout/',headers=headers,timeout=5).status_code==204
    assert session.get(url+'/api/auth/me/',timeout=5).status_code==401
    command(['systemctl','stop','inf4173-test-web.service'])
    assert command(['systemctl','show','-p','ActiveState','--value','inf4173-test-web.service'],capture=True).strip()==b'inactive'
    command(['systemctl','start','inf4173-test-web.service'])
    ok('logout, service stop and subsequent start')
    backup_test()
    ok('encrypted PostgreSQL backup, full restore, permissions and source preservation')
    summary={'scope':'temporary Linux VM, local HTTPS, fictional providers only',
             'checks_passed':checks,'gmail_calls':0,'gemini_calls':0,
             'public_deployment':False,'timer_enabled':False}
    # Only allowlisted assertions are printed; no archive/key/cookie is retained in CI artifacts.
    print(json.dumps(summary),flush=True)


def backup_test():
    import psycopg
    from cryptography.fernet import Fernet
    env=environment()
    archives=STATE/'archives';archives.mkdir(mode=0o700)
    private_write(STATE/'backup.key',Fernet.generate_key().decode())
    private_write(STATE/'backup.pgpass','127.0.0.1:55486:*:sync_test_linux_admin:public-ephemeral-linux-password\n')
    env.update(PGHOST='127.0.0.1',PGPORT='55486',PGUSER='sync_test_linux_admin',
               PGDATABASE='suivi_sync_test_linux_https',PGPASSFILE=str(STATE/'backup.pgpass'),
               BACKUP_DIR=str(archives),BACKUP_KEY_FILE=str(STATE/'backup.key'))
    archive=archives/'fiction.fbackup'
    args=[RELEASE/'backend/.venv/bin/python',RELEASE/'deploy/postgres_archive.py']
    for op,extra in [('backup',[]),('verify',[]),('restore-check',['--target','restore_check_linux_https','--owner','sync_test_linux_app'])]:
        command(args+[op,'--archive',archive]+extra,env=env)
    assert archive.stat().st_mode & 0o777==0o600 and archive.read_bytes().startswith(b'gAAAA')
    def snapshot(name):
        from psycopg import sql
        with psycopg.connect(host='127.0.0.1',port=55486,user='sync_test_linux_admin',
                            password='public-ephemeral-linux-password',dbname=name) as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            count=conn.execute('SELECT count(*) FROM gmail_integration_gmailmessage').fetchone()[0]
            rows=conn.execute('SELECT encrypted_content FROM gmail_integration_gmailmessage ORDER BY id').fetchall()
            for row in rows:Fernet(env['GMAIL_MESSAGE_ENCRYPTION_KEY'].encode()).decrypt(bytes(row[0]))
            config=conn.execute('SELECT gmail_enabled,auto_analysis_enabled FROM gmail_sync_syncconfiguration').fetchall()
            assert config==[(False,False)]
            result={}
            tables=conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
            for (table,) in tables:
                # restore-check intentionally marks the clone for operator review.
                expression="to_jsonb(t)-'blocked_code'" if table=='gmail_sync_syncconfiguration' else 'to_jsonb(t)'
                query=sql.SQL('SELECT ('+expression+')::text FROM {} t').format(sql.Identifier(table))
                digests=sorted(hashlib.sha256(r[0].encode()).hexdigest() for r in conn.execute(query))
                result[table]=(len(digests),hashlib.sha256(''.join(digests).encode()).hexdigest())
            if name.startswith('restore_check_'):
                assert conn.execute('SELECT blocked_code FROM gmail_sync_syncconfiguration').fetchone()[0]=='restore_review'
            assert conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname='sync_test_linux_app'").fetchone()==(False,False,False)
            return count,hashlib.sha256(b''.join(bytes(row[0]) for row in rows)).hexdigest(),result
    assert snapshot('suivi_sync_test_linux_https')==snapshot('restore_check_linux_https')


def cleanup():
    # These are exact test-only paths and units; nothing on the user's Windows host.
    failures=[]
    for unit in UNITS:
        try:
            subprocess.run(['systemctl','stop',unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
            state=subprocess.run(['systemctl','is-active',unit],capture_output=True,timeout=10).stdout.strip()
            if state in (b'active',b'activating',b'deactivating'):failures.append(unit)
        except subprocess.TimeoutExpired:
            failures.append(unit)
        path=Path('/etc/systemd/system')/unit
        if path.is_file():path.unlink()
    command(['systemctl','daemon-reload'])
    hosts=Path('/etc/hosts');hosts.write_text(hosts.read_text().replace(HOST_LINE,''))
    if STATE.exists():
        if STATE.resolve()!=Path('/run/inf4173-linux-test'):raise RuntimeError('Unexpected cleanup path')
        shutil.rmtree(STATE)  # Only ephemeral test secrets, never user archives.
    for port in (80,443,8001):
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(('127.0.0.1',port))==0:failures.append('port-'+str(port))
    if failures:raise RuntimeError('Temporary unit cleanup could not be confirmed')
    print('OK temporary units stopped; ephemeral test keys removed',flush=True)


if __name__=='__main__':
    try:
        guard()
        {'prepare':prepare,'test':test,'cleanup':cleanup}[sys.argv[1]]()
    except Exception as error:
        import traceback
        # Location only, never source line/local variables/raw exception arguments.
        trace=traceback.extract_tb(error.__traceback__)
        location=trace[-1] if trace else None
        print('ERROR Linux HTTPS stage failed: '+type(error).__name__+
              (' at '+Path(location.filename).name+':'+str(location.lineno) if location else '')+
              '; no raw diagnostic exported.',file=sys.stderr)
        sys.exit(1)
