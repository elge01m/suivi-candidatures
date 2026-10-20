"""Forward migration preservation checks, exclusively in Django's test database."""
from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class LotOneMigrationTests(TransactionTestCase):
    def test_existing_records_survive_without_invented_confirmation(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [('candidatures', '0001_initial')]
        executor.migrate(old_target)
        try:
            # Keep all other apps at their current schema in this isolated database.
            targets = [node for node in latest if node[0] != 'candidatures'] + old_target
            apps = MigrationExecutor(connection).loader.project_state(targets).apps
            User = apps.get_model('auth', 'User')
            Item = apps.get_model('candidatures', 'Candidature')
            user = User.objects.create(username='fiction-migration')
            items = []
            for state in ('envoyee', 'entrevue', 'offre_recue', 'offre_acceptee', 'offre_declinee', 'refusee', 'retiree'):
                item = Item.objects.create(utilisateur_id=user.pk, employeur='Fictif', poste='Test',
                    etat=state, date_candidature=date(2026, 10, 13), notes='Notes fictives conservées')
                items.append(Item.objects.values().get(pk=item.pk))
            Conn = apps.get_model('gmail_integration', 'GmailConnection')
            Message = apps.get_model('gmail_integration', 'GmailMessage')
            Analysis = apps.get_model('email_analysis', 'EmailAnalysis')
            Proposal = apps.get_model('email_analysis', 'CandidatureProposal')
            conn = Conn.objects.create(utilisateur_id=user.pk, encrypted_tokens=b'fiction-token', expires_at=timezone.now())
            message = Message.objects.create(connexion_id=conn.pk, gmail_id='fiction-only', thread_id='fiction',
                date_gmail=timezone.now(), encrypted_content=b'fiction-message')
            analysis = Analysis.objects.create(message_id=message.pk, model_name='fiction', schema_version='1', status='completed')
            proposal = Proposal.objects.create(analysis_id=analysis.pk, utilisateur_id=user.pk, result_id=items[0]['id'],
                status='acceptee', revision=3, encrypted_data=b'fiction-proposal')
            saved = {model._meta.label: list(model.objects.values()) for model in (Conn, Message, Analysis, Proposal)}
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(latest)
            self.assertEqual([(m.app_label, m.name, backwards) for m, backwards in plan],
                [('candidatures', '0002_alter_candidature_options_and_more', False),
                 ('candidatures', '0003_candidaturechange', False)])
            executor.migrate(latest)
            current = executor.loader.project_state(latest).apps
            self.assertFalse(current.get_model('candidatures', 'CandidatureChange').objects.exists())
            NewItem = current.get_model('candidatures', 'Candidature')
            for original in items:
                after = NewItem.objects.values().get(pk=original['id'])
                self.assertIsNone(after.pop('confirmation_reception'))
                self.assertEqual(after, original)
            for label, rows in saved.items():
                self.assertEqual(list(current.get_model(label).objects.values()), rows)
            self.assertEqual(current.get_model('email_analysis', 'CandidatureProposal').objects.get(pk=proposal.pk).status, 'acceptee')
        finally:
            MigrationExecutor(connection).migrate(latest)
