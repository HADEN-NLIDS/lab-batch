from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import Consent, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ('id', 'username', 'display_name', 'role', 'tenant',
                    'department', 'subject', 'status', 'deleted_at')
    list_filter = ('role', 'status', 'tenant')
    search_fields = ('username', 'display_name')
    ordering = ('id',)

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('基本信息', {'fields': ('display_name', 'role', 'tenant',
                                'department', 'subject')}),
        ('状态', {'fields': ('status', 'is_active', 'is_staff',
                            'is_superuser', 'last_login_at', 'deleted_at')}),
        ('权限', {'fields': ('groups', 'user_permissions')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'password1', 'password2', 'tenant',
                       'role', 'display_name'),
        }),
        # 员工账号必须绑定体检人，否则登录后列表为空
        ('员工档案', {'fields': ('subject', 'department')}),
    )


@admin.register(Consent)
class ConsentAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'consent_type', 'consent_version',
                    'granted_at', 'revoked_at')
    list_filter = ('consent_type',)
    search_fields = ('user__username',)
