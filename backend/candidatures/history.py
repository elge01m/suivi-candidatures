"""Business snapshots only: never copy mail text, headers or OAuth data."""
from datetime import date

from .models import CandidatureChange

FIELDS = ('employeur', 'poste', 'date_candidature', 'etat', 'confirmation_reception', 'lien', 'notes')


def snapshot(item):
    return {field: value.isoformat() if isinstance(value := getattr(item, field), date) else value for field in FIELDS}


def record_change(item, before, *, proposal=None):
    after = snapshot(item)
    changed = [field for field in FIELDS if before is None or before.get(field) != after[field]]
    if not changed:
        return None
    message = proposal.analysis.message if proposal else None
    if proposal and (proposal.utilisateur_id != item.utilisateur_id or message.connexion.utilisateur_id != item.utilisateur_id):
        raise ValueError('History owner mismatch.')
    return CandidatureChange.objects.create(candidature=item, utilisateur_id=item.utilisateur_id,
        origine='proposition' if proposal else 'manuelle',
        avant={field:before[field] for field in changed} if before is not None else {},
        apres={field:after[field] for field in changed}, source_message=message,
        source_message_reference=message.pk if message else None,
        source_proposition_reference=proposal.pk if proposal else None)
