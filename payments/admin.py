from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    Attempt,
    AuditLog,
    IntegrationConfig,
    Notification,
    Payment,
    ReconciliationRun,
    RefundEvent,
)
from .services import audit, cancel_attempt

admin.site.site_header = "Procesador SumUp"
admin.site.site_title = "Administración de pagos"
admin.site.index_title = "Supervisión de la instalación"


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


class AttemptInline(admin.TabularInline):
    model = Attempt
    extra = 0
    fields = ("id", "status", "uncertain", "sumup_id", "refunded_amount", "last_checked_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ("reference", "amount", "currency", "merchant_code", "created_at")
    search_fields = ("=reference", "=id", "attempts__sumup_id", "attempts__transaction_id")
    inlines = [AttemptInline]


@admin.register(Attempt)
class AttemptAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "payment",
        "status",
        "uncertain",
        "closed_confirmed",
        "refunded_amount",
        "last_checked_at",
        "last_error",
    )
    list_filter = ("status", "uncertain", "closed_confirmed")
    search_fields = ("=id", "payment__reference", "sumup_id", "transaction_id")
    actions = ["request_checks", "request_cancellations"]
    exclude = ("request_hash", "idempotency_key")

    def get_readonly_fields(self, request, obj=None):
        return [f for f in super().get_readonly_fields(request, obj) if f not in self.exclude]

    @admin.action(description="Solicitar comprobación con SumUp")
    def request_checks(self, request, queryset):
        for attempt in queryset:
            Attempt.objects.filter(pk=attempt.pk).update(check_requested_at=timezone.now())
            audit(request.user, "check_requested", attempt.id, "En cola para el worker")
        self.message_user(request, "Comprobaciones añadidas a la cola.")

    @admin.action(description="Solicitar cancelación de los checkouts seleccionados")
    def request_cancellations(self, request, queryset):
        for attempt in queryset:
            result = cancel_attempt(attempt.id, request.user)
            level = messages.WARNING if result.uncertain else messages.INFO
            self.message_user(
                request,
                f"{attempt.id}: {result.get_status_display()}; incierto={result.uncertain}.",
                level,
            )


@admin.register(IntegrationConfig)
class IntegrationConfigAdmin(admin.ModelAdmin):
    actions = ["request_reconciliation"]
    readonly_fields = ("reconcile_requested",)

    def has_add_permission(self, request):
        return not IntegrationConfig.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit(request.user, "config_changed", obj.pk, ", ".join(form.changed_data))

    @admin.action(description="Solicitar conciliación completa")
    def request_reconciliation(self, request, queryset):
        queryset.update(reconcile_requested=True)
        audit(request.user, "reconciliation_requested", 1, "En cola para el worker")
        self.message_user(request, "Conciliación solicitada.")


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "attempt",
        "attempts",
        "delivered_at",
        "exhausted",
        "next_attempt_at",
        "last_error",
    )
    list_filter = ("exhausted", "delivered_at")
    search_fields = ("=id", "attempt__payment__reference")
    actions = ["retry_notifications"]

    @admin.action(description="Reintentar notificaciones no entregadas (nuevo plazo de 24 h)")
    def retry_notifications(self, request, queryset):
        now = timezone.now()
        for notification in queryset.filter(delivered_at__isnull=True):
            Notification.objects.filter(pk=notification.pk).update(
                exhausted=False,
                next_attempt_at=now,
                deadline_at=now + timedelta(seconds=settings.NOTIFICATION_RETRY_SECONDS),
            )
            audit(request.user, "notification_retry", notification.id, "Nuevo plazo de 24 horas")
        self.message_user(
            request, "Reintentos solicitados; se conserva el identificador de evento."
        )


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "finished_at", "success", "checked", "errors")
    list_filter = ("success",)


@admin.register(RefundEvent)
class RefundEventAdmin(ReadOnlyAdmin):
    list_display = ("provider_id", "attempt", "amount", "status", "occurred_at")
    search_fields = ("provider_id", "attempt__payment__reference")


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "actor_name", "action", "target", "outcome")
    search_fields = ("actor_name", "action", "target")

    def has_change_permission(self, request, obj=None):
        return False
