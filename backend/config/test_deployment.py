import base64
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from .deployment import oauth_return_url, production_values
from .production_logging import MetadataOnly

ROOT=Path(__file__).resolve().parents[2]
TOKEN=base64.urlsafe_b64encode(b't'*32).decode()
MESSAGE=base64.urlsafe_b64encode(b'm'*32).decode()


def fixture_env():
    return {'DJANGO_SECRET_KEY':'Public-fiction-only-test-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ',
        'APP_ORIGIN':'https://app.example.invalid','DJANGO_ALLOWED_HOSTS':'app.example.invalid',
        'GOOGLE_REDIRECT_URI':'https://app.example.invalid/api/gmail/callback/',
        'GOOGLE_TOKEN_ENCRYPTION_KEY':TOKEN,'GMAIL_MESSAGE_ENCRYPTION_KEY':MESSAGE,
        'DJANGO_STATIC_ROOT':str(Path(tempfile.gettempdir()).resolve()/'fiction-static')}


def archive_module():
    spec=importlib.util.spec_from_file_location('archive_for_tests',ROOT/'deploy/postgres_archive.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class ProductionConfigurationTests(SimpleTestCase):
    def test_secure_defaults_and_independent_external_secret(self):
        cfg=production_values(fixture_env())
        for key in ('DEBUG','SECURE_HSTS_INCLUDE_SUBDOMAINS','SECURE_HSTS_PRELOAD'):
            self.assertFalse(cfg[key])
        for key in ('SECURE_SSL_REDIRECT','CSRF_COOKIE_SECURE','SESSION_COOKIE_SECURE','SESSION_COOKIE_HTTPONLY'):
            self.assertTrue(cfg[key])
        self.assertFalse(cfg['CSRF_COOKIE_HTTPONLY'])  # React must read this cookie, not the session cookie.
        self.assertEqual(cfg['OAUTH_SUCCESS_REDIRECT_URL'],'https://app.example.invalid/')
        self.assertEqual(cfg['SECURE_PROXY_SSL_HEADER'],('HTTP_X_FORWARDED_PROTO','https'))

    def test_missing_weak_or_control_character_secret_fails_without_values(self):
        for value in ('','short','a'*80,'django-insecure-'+'x'*80,'secret\n'):
            with self.subTest(value_length=len(value)),self.assertRaises(ImproperlyConfigured) as caught:
                production_values(fixture_env() | {'DJANGO_SECRET_KEY':value})
            self.assertNotIn(value or 'NEVER_MATCH_EMPTY',str(caught.exception))

    def test_origins_hosts_csrf_and_callback_fail_closed(self):
        cases=[{'APP_ORIGIN':'http://app.example.invalid'}, {'APP_ORIGIN':'https://user:pass@app.example.invalid'},
            {'DJANGO_ALLOWED_HOSTS':'*'}, {'DJANGO_ALLOWED_HOSTS':'.example.invalid'},
            {'DJANGO_ALLOWED_HOSTS':'other.example.invalid'},
            {'DJANGO_CSRF_TRUSTED_ORIGINS':'http://app.example.invalid'},
            {'DJANGO_CSRF_TRUSTED_ORIGINS':'https://evil.example.invalid'},
            {'GOOGLE_REDIRECT_URI':'https://app.example.invalid/api/gmail/callback/?code=fixture'},
            {'GOOGLE_REDIRECT_URI':'http://localhost:8000/api/gmail/callback/'},
            {'OAUTH_SUCCESS_REDIRECT_URL':'https://evil.example.invalid/'}]
        for changes in cases:
            with self.subTest(fields=list(changes)),self.assertRaises(ImproperlyConfigured):
                production_values(fixture_env()|changes)

    def test_fernet_keys_are_valid_and_distinct(self):
        for changes in ({'GMAIL_MESSAGE_ENCRYPTION_KEY':TOKEN},{'GMAIL_MESSAGE_ENCRYPTION_KEY':'invalid'},
                {'GOOGLE_TOKEN_ENCRYPTION_KEY':''}):
            with self.subTest(fields=list(changes)),self.assertRaises(ImproperlyConfigured):
                production_values(fixture_env()|changes)

    def test_static_root_must_be_absolute(self):
        with self.assertRaises(ImproperlyConfigured):production_values(fixture_env()|{'DJANGO_STATIC_ROOT':'relative'})

    def test_oauth_return_never_accepts_request_injection_or_external_production_origin(self):
        for url in ('//evil.invalid','javascript:alert(1)','https://app.example.invalid/?code=fixture',
                'https://app.example.invalid/#token','https://app.example.invalid\\@evil.invalid',
                'https://app.example.invalid/\r\nInjected:1','http://evil.invalid/'):
            with self.subTest(url=url),self.assertRaises(ImproperlyConfigured):oauth_return_url(url)
        self.assertEqual(oauth_return_url('/'),'/')
        self.assertEqual(oauth_return_url('http://localhost:5173/'),'http://localhost:5173/')
        with self.assertRaises(ImproperlyConfigured):
            oauth_return_url('http://localhost:5173/',production=True,origin='https://app.example.invalid')

    def test_production_logging_never_formats_sensitive_arguments_or_exceptions(self):
        record=logging.LogRecord('django.request',logging.ERROR,'fiction',1,'password=%s',('private-fixture',),None)
        record.request=object();record.exc_text='private-fixture';record.stack_info='private-fixture'
        MetadataOnly().filter(record)
        self.assertNotIn('private-fixture',record.getMessage())
        self.assertIsNone(record.request);self.assertIsNone(record.exc_text);self.assertIsNone(record.stack_info)

    def test_templates_keep_worker_opt_in_and_remove_raw_request_logs(self):
        nginx=(ROOT/'deploy/nginx/suivi.conf').read_text()
        for raw in ('$request ','$request_uri ','$args ','$http_cookie ','$http_authorization '):
            self.assertNotIn(raw,nginx.split('log_format suivi_safe')[1].split(';')[0])
        self.assertIn('error_log /dev/null',nginx)
        self.assertIn('127.0.0.1:8001',(ROOT/'deploy/gunicorn.conf.py').read_text())
        self.assertIn('DJANGO_SETTINGS_MODULE=config.production_settings',(ROOT/'deploy/systemd/suivi-sync.service').read_text())


PROD=production_values(fixture_env()) | {'GOOGLE_CLIENT_ID':'fixture','GOOGLE_CLIENT_SECRET':'fixture',
    'GOOGLE_TOKEN_ENCRYPTION_KEY':TOKEN,'GMAIL_MESSAGE_ENCRYPTION_KEY':MESSAGE,
    'PASSWORD_HASHERS':['django.contrib.auth.hashers.MD5PasswordHasher']}


@override_settings(**PROD)
class ProductionHTTPTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('production-fiction',password='fiction-only')
        self.client=Client(enforce_csrf_checks=True,HTTP_HOST='app.example.invalid')

    def csrf(self):
        r=self.client.get('/api/auth/csrf/',secure=True)
        self.assertEqual(r.status_code,200)
        return {'HTTP_X_CSRFTOKEN':self.client.cookies['csrftoken'].value,'HTTP_ORIGIN':'https://app.example.invalid'}

    def login(self):
        r=self.client.post('/api/auth/login/',json.dumps({'username':'production-fiction','password':'fiction-only'}),
            content_type='application/json',secure=True,**self.csrf())
        self.assertEqual(r.status_code,200)

    def test_http_redirect_and_unknown_host(self):
        r=self.client.get('/api/auth/csrf/')
        self.assertEqual(r.status_code,301)
        self.assertEqual(r['Location'],'https://app.example.invalid/api/auth/csrf/')
        self.assertEqual(self.client.get('/api/auth/csrf/',secure=True,HTTP_HOST='evil.invalid').status_code,400)

    def test_secure_session_csrf_rotation_and_headers(self):
        headers=self.csrf();before=headers['HTTP_X_CSRFTOKEN'];self.login()
        self.assertNotEqual(before,self.client.cookies['csrftoken'].value)
        cookie=self.client.cookies['sessionid'];self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly'])
        self.assertTrue(self.client.cookies['csrftoken']['secure'])
        r=self.client.get('/api/auth/me/',secure=True)
        self.assertEqual(r.status_code,200);self.assertEqual(r['X-Content-Type-Options'],'nosniff')
        self.assertEqual(r['Referrer-Policy'],'no-referrer');self.assertIn('max-age=3600',r['Strict-Transport-Security'])

    def test_anonymous_login_and_authenticated_writes_require_csrf_and_same_origin(self):
        self.assertEqual(self.client.post('/api/auth/login/','{}',content_type='application/json',secure=True).status_code,403)
        self.login()
        data={'employeur':'Fiction','poste':'Fiction','etat':'envoyee','date_candidature':'2026-10-19'}
        self.assertEqual(self.client.post('/api/candidatures/',data,content_type='application/json',secure=True).status_code,403)
        headers=self.csrf();headers['HTTP_ORIGIN']='https://evil.invalid'
        self.assertEqual(self.client.post('/api/candidatures/',data,content_type='application/json',secure=True,**headers).status_code,403)
        headers['HTTP_ORIGIN']='https://app.example.invalid'
        self.assertEqual(self.client.post('/api/candidatures/',data,content_type='application/json',secure=True,**headers).status_code,201)
        self.assertEqual(self.client.post('/api/auth/logout/',secure=True).status_code,403)
        self.assertEqual(self.client.post('/api/auth/logout/',secure=True,**headers).status_code,204)

    def test_callback_uses_configured_clean_https_destination(self):
        self.login()
        self.assertEqual(self.client.post('/api/gmail/connect/',secure=True,**self.csrf()).status_code,302)
        pending=self.client.session['gmail_oauth_pending']
        with patch('gmail_integration.views.exchange_code') as exchange:
            r=self.client.get('/api/gmail/callback/',{'state':pending['state'],'code':'fixture'},secure=True)
        self.assertEqual(r.status_code,302);self.assertEqual(r['Location'],'https://app.example.invalid/')
        exchange.assert_called_once()

    def test_invalid_return_destination_prevents_exchange(self):
        self.login();self.client.post('/api/gmail/connect/',secure=True,**self.csrf())
        pending=self.client.session['gmail_oauth_pending']
        with override_settings(OAUTH_SUCCESS_REDIRECT_URL='https://evil.invalid/'),patch('gmail_integration.views.exchange_code') as exchange:
            r=self.client.get('/api/gmail/callback/',{'state':pending['state'],'code':'fixture'},secure=True)
        self.assertEqual(r.status_code,503);exchange.assert_not_called()


class ArchiveSafetyTests(SimpleTestCase):
    def test_tampered_ciphertext_and_wrong_key_cannot_be_restored(self):
        mod=archive_module();cipher=Fernet(base64.urlsafe_b64encode(b'b'*32))
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name);file=folder/'fiction.fbackup';data=cipher.encrypt(b'fictional archive')
            file.write_bytes(data);os.chmod(file,0o600)
            digest=Path(str(file)+'.sha256');import hashlib
            digest.write_text(hashlib.sha256(data).hexdigest());os.chmod(digest,0o600)
            self.assertEqual(mod.decode_archive(file,folder.resolve(),cipher),b'fictional archive')
            with self.assertRaises(InvalidToken):mod.decode_archive(file,folder.resolve(),Fernet(base64.urlsafe_b64encode(b'c'*32)))
            file.write_bytes(data[:-1]+b'x')
            with self.assertRaises(mod.ArchiveError):mod.decode_archive(file,folder.resolve(),cipher)

    def test_backup_does_not_overwrite_existing_archive(self):
        mod=archive_module()
        with tempfile.TemporaryDirectory() as name:
            file=Path(name)/'existing.fbackup';file.write_bytes(b'keep')
            with patch.object(mod.subprocess,'Popen') as process,self.assertRaises(mod.ArchiveError):
                mod.backup(Path(name).resolve(),None,{},file)
            process.assert_not_called();self.assertEqual(file.read_bytes(),b'keep')

    def test_restore_requires_new_explicitly_named_database_before_connecting(self):
        mod=archive_module()
        for target in ('suivi_candidatures','','restore_check_a;DROP DATABASE postgres'):
            with self.subTest(target=target),self.assertRaises(mod.ArchiveError):
                mod.restore_check(b'',{'PGDATABASE':'suivi_candidatures'},target)

    def test_remote_backup_requires_verified_tls_before_reading_credentials(self):
        mod=archive_module()
        env={key:'fixture' for key in ('PGUSER','PGDATABASE','PGPASSFILE','BACKUP_KEY_FILE','BACKUP_DIR')}
        env.update(PGHOST='database.example.invalid',PGPORT='5432',PGSSLMODE='disable')
        with self.assertRaises(mod.ArchiveError) as caught:mod.configuration(env)
        self.assertIn('verified TLS',str(caught.exception))

    def test_password_environment_and_invalid_owner_are_refused(self):
        mod=archive_module()
        env={key:'fixture' for key in ('PGUSER','PGDATABASE','PGPASSFILE','BACKUP_KEY_FILE','BACKUP_DIR')}
        env.update(PGHOST='127.0.0.1',PGPORT='5432',PGPASSWORD='fiction')
        with self.assertRaises(mod.ArchiveError) as caught:mod.configuration(env)
        self.assertIn('PGPASSFILE',str(caught.exception))
        with self.assertRaises(mod.ArchiveError):
            mod.restore_check(b'',{'PGDATABASE':'source','PGUSER':'operator'},'restore_check_fiction','invalid;role')


class ArchivePostgreSQLTests(TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_encrypted_backup_full_restore_and_existing_target_protection(self):
        cfg=connection.settings_dict
        self.assertTrue(cfg['NAME'].startswith('test_suivi_sync_test_'))
        self.assertNotEqual(str(cfg['PORT']),'5432')
        from candidatures.models import Candidature
        from gmail_integration.models import GmailConnection
        from gmail_sync.models import SyncConfiguration
        user=get_user_model().objects.create_user('backup-fiction')
        Candidature.objects.create(utilisateur=user,employeur='Backup fiction',poste='Test',date_candidature='2026-10-19')
        conn=GmailConnection.objects.create(utilisateur=user,encrypted_tokens=b'fiction',expires_at=timezone.now())
        SyncConfiguration.objects.create(connexion=conn,gmail_enabled=True,auto_analysis_enabled=True)
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name);backups=folder/'archives';backups.mkdir(mode=0o700)
            key=folder/'backup.key';key.write_bytes(base64.urlsafe_b64encode(b'b'*32));os.chmod(key,0o600)
            pgpass=folder/'fixture.pgpass'
            escape=lambda value:str(value).replace('\\','\\\\').replace(':','\\:')
            pgpass.write_text(':'.join(map(escape,[cfg['HOST'],cfg['PORT'],'*',cfg['USER'],cfg['PASSWORD']]))+'\n')
            os.chmod(pgpass,0o600)
            env=os.environ | {'PGHOST':cfg['HOST'],'PGPORT':str(cfg['PORT']),'PGUSER':cfg['USER'],'PGDATABASE':cfg['NAME'],
                'PGPASSFILE':str(pgpass),'BACKUP_DIR':str(backups),'BACKUP_KEY_FILE':str(key),'BACKUP_ISOLATED_TEST':'1'}
            archive=backups/'fiction.fbackup';target='restore_check_lot7_'+uuid.uuid4().hex[:8]
            owner='sync_test_restore_owner_'+uuid.uuid4().hex[:8]
            import psycopg
            from psycopg import sql
            with psycopg.connect(host=cfg['HOST'],port=cfg['PORT'],user=cfg['USER'],password=cfg['PASSWORD'],dbname=cfg['NAME']) as admin:
                admin.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(owner)))
            command=[sys.executable,'-B',str(ROOT/'deploy/postgres_archive.py')]
            for operation,extra in [('backup',[]),('verify',[]),('restore-check',['--target',target,'--owner',owner])]:
                result=subprocess.run(command+[operation,'--archive',str(archive)]+extra,env=env,capture_output=True,text=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(archive.read_bytes().startswith(b'gAAAA'))
            with psycopg.connect(host=cfg['HOST'],port=cfg['PORT'],user=cfg['USER'],password=cfg['PASSWORD'],dbname=target) as clone:
                self.assertEqual(clone.execute('SELECT count(*) FROM candidatures_candidature').fetchone()[0],1)
                self.assertEqual(clone.execute('SELECT gmail_enabled,auto_analysis_enabled,blocked_code FROM gmail_sync_syncconfiguration').fetchone(),(False,False,'restore_review'))
                self.assertEqual(clone.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='candidatures_candidature'::regclass").fetchone()[0],owner)
                self.assertEqual(clone.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=%s',(owner,)).fetchone(),(False,False,False))
            result=subprocess.run(command+['restore-check','--archive',str(archive),'--target',target],env=env,capture_output=True,timeout=120)
            self.assertEqual(result.returncode,1)
            self.assertEqual(Candidature.objects.count(),1)
            self.assertTrue(SyncConfiguration.objects.get().gmail_enabled)  # Source not changed.
