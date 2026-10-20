"""Portable checks: real environment guards and socket policy, no DB/provider."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
spec=importlib.util.spec_from_file_location('linux_fixture_entry',HERE/'entry.py')
entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
spec=importlib.util.spec_from_file_location('linux_fixture_runner',HERE/'run.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


class LocalSafetyTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()
