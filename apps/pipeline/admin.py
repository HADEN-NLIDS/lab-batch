from django.contrib import admin

from apps.pipeline.models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'report', 'task_type', 'status', 'attempts',
                    'max_attempts', 'scheduled_at', 'finished_at')
    list_filter = ('task_type', 'status')
    search_fields = ('error_msg',)
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        # 任务由上传或处理链路创建，不手工添加
        return False
