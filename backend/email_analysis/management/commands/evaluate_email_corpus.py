import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from email_analysis.evaluation.runner import evaluate


class Command(BaseCommand):
    help='Evaluate the fixed 40-email fictitious corpus in isolated PostgreSQL only.'

    def add_arguments(self,parser):
        parser.add_argument('--mode',choices=['simulated','real'],default='simulated')
        parser.add_argument('--run-id',required=True)
        parser.add_argument('--output',required=True)
        parser.add_argument('--allow-real-gemini',action='store_true')
        parser.add_argument('--max-calls',type=int,default=1)

    def handle(self,*args,**options):
        if settings.SETTINGS_MODULE!='config.evaluation_settings':
            raise CommandError('Use config.evaluation_settings and a dedicated isolated PostgreSQL database.')
        try:
            output=Path(options['output'])
            # Keep simulated and actual reports separate, including across invocations.
            if output.exists():
                old=json.loads(output.read_text(encoding='utf-8'))
                if old.get('mode')!=options['mode'] or old.get('run_id')!=options['run_id']:
                    raise ValueError('Do not overwrite a different campaign or measurement mode.')
            if not output.parent.is_dir():
                raise ValueError('Output directory must already exist.')
            result=evaluate(options['run_id'],options['mode'],allow_real=options['allow_real_gemini'],max_calls=options['max_calls'])
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        except Exception:
            raise CommandError('Evaluation stopped; verify isolation, campaign configuration, quotas and output path. Sensitive details suppressed.') from None
        self.stdout.write(json.dumps({'mode':result['mode'],'completed':result['completed'],
            'new_attempts':result['new_attempts_this_invocation'],'stop_reason':result['stop_reason']}))
