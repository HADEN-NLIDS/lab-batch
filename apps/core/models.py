from django.db import models
from django.utils import timezone


class Organization(models.Model):
    """组织（租户）。"""

    name = models.CharField(max_length=128)
    code = models.CharField(max_length=64, unique=True,
                            help_text='组织编码，登录时定位租户')
    contact = models.CharField(max_length=64, blank=True, default='')
    status = models.SmallIntegerField(default=1, help_text='1 启用 0 停用')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'organization'
        verbose_name = '组织'
        verbose_name_plural = '组织'

    def __str__(self):
        return self.name


class AuditLog(models.Model):
    """关键操作审计日志。只增不改，不设外键以便保留历史。"""

    ACTION_CHOICES = [
        ('login', '登录'),
        ('view', '查看'),
        ('export', '导出'),
        ('delete', '删除'),
        ('upload', '上传'),
    ]

    tenant_id = models.BigIntegerField()
    user_id = models.BigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=32, choices=ACTION_CHOICES)
    target_type = models.CharField(max_length=32, blank=True, default='')
    target_id = models.BigIntegerField(null=True, blank=True)
    detail = models.CharField(max_length=512, blank=True, default='')
    ip = models.CharField(max_length=45, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'audit_log'
        verbose_name = '审计日志'
        verbose_name_plural = '审计日志'
        indexes = [
            models.Index(fields=['tenant_id', 'created_at'],
                         name='idx_tenant_time'),
            models.Index(fields=['user_id', 'created_at'],
                         name='idx_user_time'),
        ]

    def __str__(self):
        return '%s %s' % (self.action, self.created_at)
