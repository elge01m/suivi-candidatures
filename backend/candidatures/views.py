from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from django.db import transaction
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet

from .models import Candidature
from .serializers import CandidatureSerializer
from .history import snapshot, record_change


@method_decorator(csrf_protect, name='dispatch')
class CandidatureViewSet(ModelViewSet):
    serializer_class = CandidatureSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Candidature.objects.filter(utilisateur=self.request.user)
        return qs.select_for_update() if self.action in ('update', 'partial_update') else qs

    @transaction.atomic
    def perform_create(self, serializer):
        item = serializer.save(utilisateur=self.request.user)
        record_change(item, None)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        before = snapshot(serializer.instance)
        item = serializer.save()
        record_change(item, before)

    @action(detail=True, methods=['get'], url_path='historique')
    def history(self, request, pk=None):
        item = self.get_object()
        rows = item.historique.filter(utilisateur=request.user)
        return Response({'results':list(rows.values('id', 'date', 'origine', 'avant', 'apres',
            'source_message_reference', 'source_proposition_reference', 'source_message_id'))}, headers={'Cache-Control':'no-store'})
