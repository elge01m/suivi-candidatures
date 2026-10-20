import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F

from gmail_sync.models import SyncConfiguration
from gmail_sync.services import process_connection


class Command(BaseCommand):
    help = 'Process persistent Gmail/analysis work, bounded; schedule once per minute.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Count configurations only; no writes or provider calls.')
        parser.add_argument('--max-seconds', type=int, default=45)
        parser.add_argument('--max-accounts', type=int, default=10)

    def handle(self, *args, **options):
        if not 1 <= options['max_seconds'] <= 50 or not 1 <= options['max_accounts'] <= 100:
            raise CommandError('Invalid worker limits.')
        ids = list(SyncConfiguration.objects.filter(connexion__utilisateur__is_active=True)
            .order_by(F('last_served_at').asc(nulls_first=True), 'pk').values_list('pk', flat=True)[:options['max_accounts']])
        if options['dry_run']:
            self.stdout.write(f'Configurations: {len(ids)}. Dry run: no changes, no external calls.')
            return
        deadline, processed, errors = time.monotonic() + options['max_seconds'], 0, 0
        for config_id in ids:
            if time.monotonic() >= deadline:
                break
            try:
                processed += int(process_connection(config_id))
            except Exception:
                # No traceback: provider errors may contain mail, headers or tokens.
                errors += 1
        self.stdout.write(f'Operations: {processed}. Errors: {errors}.')
        if errors:
            raise CommandError('Some jobs could not be processed; sensitive details suppressed.')
