"""Portable checks: real environment guards and socket policy, no DB/provider."""
import ast
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('linux_fixture_entry',HERE/'entry.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
spec=importlib.util.spec_from_file_location('linux_fixture_runner',HERE/'run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class LocalSafetyTests(unittest.TestCase):
    def test_http_probe_returns_only_status_not_body_headers_or_cookies(self):
        class Response:
            status_code=502
            @property
            def text(self):raise AssertionError('Body must not be inspected')
            @property
            def headers(self):raise AssertionError('Headers must not be inspected')
            @property
            def cookies(self):raise AssertionError('Cookies must not be inspected')
        session=Mock();session.get.return_value=Response()
        self.assertEqual(runner.probe_http(session,'https://fixture.invalid'),{'http_status':502})

    def test_http_probe_uses_fixed_error_categories_without_exception_details(self):
        import requests
        for error,category in [(requests.exceptions.SSLError('fiction-secret'),'tls_error'),
                               (requests.exceptions.Timeout('fiction-secret'),'timeout'),
                               (requests.exceptions.ConnectionError('fiction-secret'),'connection_error')]:
            session=Mock();session.get.side_effect=error
            with self.subTest(category=category):
                self.assertEqual(runner.probe_http(session,'https://fixture.invalid'),{'error':category})

    def test_https_failure_keeps_original_attempts_and_emits_last_result(self):
        import requests
        for result in (requests.exceptions.SSLError('fiction-secret'),Mock(status_code=502)):
            session=Mock()
            if isinstance(result,Exception):session.get.side_effect=result;expected={'error':'tls_error'}
            else:session.get.return_value=result;expected={'http_status':502}
            with self.subTest(expected=expected),patch.object(runner.time,'sleep') as sleep,patch.object(runner,'readiness_diagnostic') as diagnose:
                with self.assertRaises(RuntimeError):runner.wait_for_https(session,'https://fixture.invalid')
                self.assertEqual(session.get.call_count,30)
                self.assertEqual(sleep.call_count,30)
                self.assertTrue(all(c.kwargs['timeout']==2 for c in session.get.call_args_list))
                diagnose.assert_called_once_with(expected)

    def test_successful_readiness_does_not_collect_diagnostics(self):
        session=Mock();session.get.return_value.status_code=200
        with patch.object(runner,'readiness_diagnostic') as diagnose,patch.object(runner.time,'sleep') as sleep:
            runner.wait_for_https(session,'https://fixture.invalid')
        diagnose.assert_not_called();sleep.assert_not_called()

    def test_systemd_metadata_filters_unexpected_and_sensitive_values(self):
        raw=b'ActiveState=failed\nSubState=fiction-secret\nResult=exit-code\nExecMainStatus=203\nNRestarts=2\nEnvironment=fiction-secret\n'
        with patch.object(runner.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=raw)) as run:
            data=runner.unit_diagnostic(runner.UNITS[0])
        self.assertEqual(data,{'ActiveState':'failed','SubState':'unavailable','Result':'exit-code','ExecMainStatus':203,'NRestarts':2})
        self.assertEqual(run.call_args.kwargs['stderr'],subprocess.DEVNULL)
        with patch.object(runner.subprocess,'run',side_effect=subprocess.TimeoutExpired('fiction-secret',2)):
            self.assertEqual(set(runner.unit_diagnostic(runner.UNITS[0]).values()),{'unavailable'})

    def test_stopped_backend_diagnostic_is_sanitized_and_uses_expected_proxy_headers(self):
        import requests
        marker='fiction-cookie-token-body-environment-secret'
        session=Mock();session.get.side_effect=requests.exceptions.ConnectionError(marker)
        factory=Mock();factory.return_value.__enter__=Mock(return_value=session);factory.return_value.__exit__=Mock(return_value=False)
        output=io.StringIO()
        with patch('requests.Session',factory),patch.object(runner,'unit_diagnostic',return_value={'ActiveState':'failed'}),patch.object(runner,'local_port_listening',side_effect=lambda port:port==443),contextlib.redirect_stdout(output):
            runner.readiness_diagnostic({'http_status':502,'body':marker,'cookie':marker})
        data=json.loads(output.getvalue().split(' ',1)[1])
        self.assertNotIn(marker,output.getvalue())
        self.assertIsNone(data['gunicorn_http_status'])
        self.assertEqual(data['https_last_probe'],{'http_status':502})
        self.assertEqual(data['listening'],{'443':True,'8001':False})
        self.assertFalse(session.trust_env)
        args=session.get.call_args
        self.assertEqual(args.args[0],'http://127.0.0.1:8001/api/auth/csrf/')
        self.assertFalse(args.kwargs['allow_redirects'])
        self.assertEqual(args.kwargs['headers'],{'Host':runner.HOST,'X-Forwarded-Proto':'https','X-Forwarded-For':'127.0.0.1','X-Real-IP':'127.0.0.1'})

    def test_port_diagnostic_only_uses_fixed_loopback_ports(self):
        with patch.object(runner.socket,'socket') as socket_factory:
            probe=socket_factory.return_value.__enter__.return_value;probe.connect_ex.return_value=111
            self.assertFalse(runner.local_port_listening(8001))
            probe.connect_ex.assert_called_once_with(('127.0.0.1',8001))
            with self.assertRaises(ValueError):runner.local_port_listening(5432)

    def test_prepare_reaches_site_package_loading_without_local_name_shadowing(self):
        class StopBeforeAnyDatabaseAccess(Exception):
            pass

        packages=runner.RELEASE/'backend/.venv/lib/python3.13/site-packages'
        with patch.object(runner.Path,'exists',return_value=False),patch.object(runner.Path,'mkdir'),patch.object(runner.Path,'glob',return_value=iter([packages])),patch.object(runner.socket,'socket'),patch.object(runner,'command',return_value=b''),patch.object(runner.shutil,'copytree'),patch.object(runner.site,'addsitedir',side_effect=StopBeforeAnyDatabaseAccess) as load:
            with self.assertRaises(StopBeforeAnyDatabaseAccess):runner.prepare()
        load.assert_called_once_with(str(packages))

    def test_main_diagnostic_omits_exception_message_and_environment(self):
        tree=ast.parse((HERE/'run.py').read_text(encoding='utf-8'))
        main=next(n for n in tree.body if isinstance(n,ast.If) and ast.unparse(n.test)=="__name__ == '__main__'")
        marker='fiction-sensitive-value-never-export'
        def fail():raise RuntimeError(marker)
        output=io.StringIO()
        context={'__name__':'__main__','guard':fail,'sys':sys,'Path':Path}
        with contextlib.redirect_stderr(output),self.assertRaises(SystemExit):
            exec(compile(ast.Module(body=[main],type_ignores=[]),'diagnostic-test','exec'),context)
        self.assertIn('RuntimeError',output.getvalue())
        self.assertNotIn(marker,output.getvalue())

    def valid(self):
        return dict(LINUX_HTTPS_FIXTURE='1',DJANGO_SETTINGS_MODULE='config.production_settings',
            DJANGO_LOAD_DOTENV='0',DB_HOST='127.0.0.1',DB_PORT='55486',
            DB_NAME='suivi_sync_test_linux_https',DB_USER='sync_test_linux_app',
            GEMINI_API_KEY='fiction-never-used',GOOGLE_CLIENT_SECRET='fiction-never-used')

    def test_accepts_only_explicit_fictional_environment(self):
        entry.check_environment(self.valid())
        for key in self.valid():
            env=self.valid();env[key]='wrong'
            with self.subTest(key=key),self.assertRaises(RuntimeError):entry.check_environment(env)

    def test_network_guard_blocks_ipv4_ipv6_and_connect_ex_without_network(self):
        # Child process only: never change the test runner's socket implementation.
        code='''import importlib.util,socket,sys
s=importlib.util.spec_from_file_location('entry',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.install_network_guard()
for family,address in ((socket.AF_INET,('8.8.8.8',443)),(socket.AF_INET6,('2001:4860:4860::8888',443))):
 for method in ('connect','connect_ex'):
  with socket.socket(family) as sock:
   try:getattr(sock,method)(address)
   except RuntimeError:pass
   else:raise AssertionError('External connection not blocked')
with socket.socket() as server:
 server.bind(('127.0.0.1',0));server.listen()
 with socket.socket() as client:client.connect(server.getsockname())
print('OK')'''
        r=subprocess.run([sys.executable,'-B','-c',code,str(HERE/'entry.py')],capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr.decode())

    def test_live_preparation_refuses_non_ci_hosts_before_any_action(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}):
            with self.assertRaises(RuntimeError):runner.guard()

    def test_worker_uses_actual_command_and_never_enables_configuration(self):
        text=(HERE/'entry.py').read_text()
        self.assertIn("call_command('process_sync_jobs'",text)
        self.assertIn("patch('gmail_integration.import_service.GmailClient', FakeGmail)",text)
        self.assertIn('forbidden_gemini',text)
        self.assertNotIn('gmail_enabled=True)',text.split("elif action == 'seed':")[1].split("elif action == 'worker':")[0])

    def test_only_correct_route_is_used(self):
        text=(HERE/'run.py').read_text()
        self.assertIn('/api/sync/runs/',text)
        self.assertNotIn('/api/sync/now/',text)

    def test_cleanup_continues_after_one_stop_timeout(self):
        def fake(args,**kwargs):
            if args[:2]==['systemctl','stop'] and args[-1]==runner.UNITS[0]:
                raise subprocess.TimeoutExpired(args,45)
            return subprocess.CompletedProcess(args,0,stdout=b'inactive')
        with patch.object(runner.subprocess,'run',side_effect=fake) as run,patch.object(runner.Path,'is_file',return_value=False),patch.object(runner.Path,'exists',return_value=False),patch.object(runner.Path,'read_text',return_value='127.0.0.1 localhost\n'),patch.object(runner.Path,'write_text'),patch.object(runner.socket,'socket') as sock:
            sock.return_value.__enter__.return_value.connect_ex.return_value=1
            with self.assertRaises(RuntimeError):runner.cleanup()
        stopped=[c.args[0][-1] for c in run.call_args_list if c.args[0][:2]==['systemctl','stop']]
        self.assertEqual(stopped,list(runner.UNITS))

    def test_existing_ci_is_not_replaced_and_no_workflow_auto_trigger(self):
        text=(ROOT/'.github/workflows/linux-https.yml').read_text()
        self.assertIn('workflow_dispatch:',text)
        self.assertIn('if: always()',text)
        self.assertNotIn('secrets.',text)
        self.assertNotIn('upload-artifact',text)
        self.assertNotIn('schedule:',text)
        self.assertNotIn('push:',text)

    def test_scripts_compile_and_generated_fake_payload_has_no_attachment(self):
        for path in HERE.glob('*.py'):ast.parse(path.read_text(encoding='utf-8'))
        self.assertNotIn('attachmentId',(HERE/'entry.py').read_text())


class TemporaryCertificateTests(unittest.TestCase):
    """Real certificate generation and strict TLS through MemoryBIO, no network."""
    @classmethod
    def setUpClass(cls):
        candidates=[shutil.which('openssl'),'C:/Program Files/Git/usr/bin/openssl.exe',
                    'C:/Program Files/Git/mingw64/bin/openssl.exe']
        cls.openssl=next((str(p) for p in candidates if p and Path(p).is_file()),None)
        if cls.openssl is None:raise RuntimeError('OpenSSL required for local certificate tests')
        cls.temp=tempfile.TemporaryDirectory(prefix='inf4173-fiction-cert-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder=Path(cls.temp.name)
        # Execute the actual certificate-generation statements from prepare(),
        # with only paths/command IO redirected to private temporary fixtures.
        tree=ast.parse((HERE/'run.py').read_text(encoding='utf-8'))
        prepare=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='prepare')
        first=next(i for i,n in enumerate(prepare.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='openssl' for t in n.targets))
        last=next(i for i,n in enumerate(prepare.body[first+1:],first+1) if isinstance(n,ast.For))
        generation=compile(ast.Module(body=prepare.body[first:last],type_ignores=[]),'actual-ca-generation','exec')
        for kind in ('valid','missing_usage','other_ca'):
            state=cls.folder/kind;state.mkdir()
            def command(args, **kwargs):
                args=list(map(str,args));args[0]=cls.openssl
                if kind=='missing_usage' and args[1:3]==['req','-x509']:
                    index=args.index('keyUsage=critical,keyCertSign,cRLSign')
                    del args[index-1:index+1]
                result=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                if result.returncode:raise RuntimeError('Fictional certificate generation failed')
            def private_write(path,text):
                path.write_text(text,encoding='utf-8');path.chmod(0o600)
            exec(generation,{'STATE':state,'HOST':runner.HOST,'command':command,'private_write':private_write})
            for path in state.iterdir():path.chmod(0o600)

    def handshake(self,kind='valid',*,trusted='valid',hostname=None):
        from urllib3.util.ssl_ import create_urllib3_context
        client_context=create_urllib3_context()
        self.assertTrue(client_context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertEqual(client_context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(client_context.check_hostname)
        client_context.load_verify_locations(cafile=str(self.folder/trusted/'ca.crt'))
        server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(self.folder/kind/'server.crt'),str(self.folder/kind/'server.key'))
        ci,co,si,so=[ssl.MemoryBIO() for _ in range(4)]
        peers=[client_context.wrap_bio(ci,co,server_hostname=hostname or runner.HOST),
               server_context.wrap_bio(si,so,server_side=True)]
        done=set()
        for _ in range(30):
            for index,peer in enumerate(peers):
                try:peer.do_handshake();done.add(index)
                except ssl.SSLWantReadError:pass
            if co.pending:si.write(co.read())
            if so.pending:ci.write(so.read())
            if len(done)==2:return
        self.fail('TLS handshake did not complete')

    def test_ca_has_critical_certificate_signing_key_usage(self):
        r=subprocess.run([self.openssl,'x509','-in',str(self.folder/'valid/ca.crt'),'-noout','-ext','keyUsage'],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0)
        self.assertIn('X509v3 Key Usage: critical',r.stdout)
        self.assertIn('Certificate Sign',r.stdout)
        self.assertIn('CRL Sign',r.stdout)

    def test_valid_chain_passes_strict_tls(self):
        self.handshake()

    def test_ca_without_key_usage_is_rejected_by_strict_tls(self):
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            self.handshake('missing_usage',trusted='missing_usage')
        self.assertEqual(caught.exception.verify_code,92)

    def test_unrecognized_authority_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):self.handshake(trusted='other_ca')

    def test_wrong_hostname_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):self.handshake(hostname='wrong.example.invalid')


if __name__=='__main__':unittest.main()
