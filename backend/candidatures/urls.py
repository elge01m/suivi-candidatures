from rest_framework.routers import DefaultRouter

from .views import CandidatureViewSet


router = DefaultRouter()
router.register('candidatures', CandidatureViewSet, basename='candidature')

urlpatterns = router.urls
