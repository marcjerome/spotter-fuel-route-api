from django.contrib import admin

from .models import RoutePlanJob


@admin.register(RoutePlanJob)
class RoutePlanJobAdmin(admin.ModelAdmin):
    list_display = ["id", "__str__", "status", "created_at", "finished_at"]
    list_filter = ["status"]
    readonly_fields = [f.name for f in RoutePlanJob._meta.fields]
