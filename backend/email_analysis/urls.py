from django.urls import path
from . import views

urlpatterns = [
    path('analyses/', views.analyses, name='analysis-list'),
    path('analyses/<uuid:analysis_id>/', views.analysis_detail, name='analysis-detail'),
    path('analyses/<uuid:analysis_id>/retry/', views.retry_analysis, name='analysis-retry'),
    path('propositions/', views.proposals, name='proposal-list'),
    path('propositions/<uuid:proposal_id>/', views.proposal_detail, name='proposal-detail'),
    path('propositions/<uuid:proposal_id>/accept/', views.accept, name='proposal-accept'),
    path('propositions/<uuid:proposal_id>/reject/', views.reject, name='proposal-reject'),
]
