from django.contrib import admin

from apps.core.models import AuditLog, Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'code', 'status', 'created_at')
    search_fields = ('name', 'code')
    list_filter = ('status',)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'tenant_id', 'user_id', 'action',
                    'target_type', 'target_id', 'created_at')
    list_filter = ('action', 'target_type')
    search_fields = ('detail',)
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        # 审计日志只由系统写入，不允许手工添加
        return False

    def has_change_permission(self, request, obj=None):
        return False
