from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"

    def ready(self):
        # 注册删除信号，导入即生效
        from apps.reports import signals  # noqa: F401
