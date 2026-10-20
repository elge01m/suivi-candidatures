from rest_framework import serializers

from .models import Candidature


class CandidatureSerializer(serializers.ModelSerializer):
    def get_fields(self):
        fields = super().get_fields()
        if self.instance is None and getattr(self, 'initial_data', {}).get('etat', 'envoyee') != 'a_preparer':
            fields['date_candidature'].required = True
        return fields

    def validate(self, attrs):
        etat = attrs.get('etat', self.instance.etat if self.instance else Candidature.Etat.ENVOYEE)
        date = attrs.get('date_candidature', self.instance.date_candidature if self.instance else None)
        confirmation = attrs.get('confirmation_reception', self.instance.confirmation_reception if self.instance else None)
        errors = {}
        if etat != Candidature.Etat.A_PREPARER and date is None:
            errors['date_candidature'] = 'Renseignez la date de candidature avant de quitter À préparer.'
        if etat == Candidature.Etat.A_PREPARER and confirmation is True:
            errors['confirmation_reception'] = 'Une candidature à préparer ne peut pas avoir été reçue.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    class Meta:
        model = Candidature
        fields = [
            'id', 'utilisateur', 'employeur', 'poste', 'date_candidature',
            'etat', 'confirmation_reception', 'lien', 'notes', 'date_creation', 'date_modification',
        ]
        read_only_fields = ['id', 'utilisateur', 'date_creation', 'date_modification']
