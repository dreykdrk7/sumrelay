from django.contrib import admin
from django.urls import path

from payments import views

admin.site.site_header = "SumRelay"
admin.site.site_title = "SumRelay"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", views.health),
    path("api/v1/payments/", views.payments),
    path("api/v1/payments/<uuid:payment_id>/", views.payment_detail),
    path("api/v1/payments/<uuid:payment_id>/cancel/", views.payment_cancel),
    path("api/v1/card-setups/", views.card_setups),
    path("api/v1/card-setups/<uuid:card_id>/", views.card_detail),
    path("api/v1/recurring-payments/", views.recurring_payments),
    path("api/v1/recurring-payments/cancel/", views.recurring_cancel),
    path("api/v1/openapi.json", views.api_schema),
    path("webhooks/sumup/", views.sumup_webhook),
]
