from django.db import models
from django.utils import timezone


class Task(models.Model):
    """异步任务队列。取任务用 SELECT ... FOR UPDATE SKIP LOCKED。

    scheduled_at 是退避重试的关键：失败后退回 pending 并推迟到期时间，
    避免立即被重新捞起。
    """

    TYPE_CHOICES = [
        ('ocr', '识别'),
        ('parse', '解析'),
        ('judge', '判定'),
        ('explain', '解读'),
    ]
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_DONE = 'done'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待处理'),
        (STATUS_RUNNING, '处理中'),
        (STATUS_DONE, '已完成'),
        (STATUS_FAILED, '失败'),
    ]

    tenant_id = models.BigIntegerField()
    report = models.ForeignKey('reports.Report', on_delete=models.CASCADE,
                               db_column='report_id', related_name='tasks')
    task_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)
    error_code = models.CharField(max_length=50, blank=True, default='')
    error_msg = models.TextField(blank=True, default='')
    worker_id = models.CharField(max_length=64, blank=True, default='')
    scheduled_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'task'
        verbose_name = '任务'
        verbose_name_plural = '任务'
        indexes = [
            models.Index(fields=['status', 'scheduled_at'],
                         name='idx_pick'),
            models.Index(fields=['tenant_id'], name='idx_tenant'),
        ]

    def __str__(self):
        return '%s %s' % (self.task_type, self.status)
