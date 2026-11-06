"""清理超期的审计日志。

用法：
    python manage.py purge_audit --days 7          # 实际执行
    python manage.py purge_audit --days 7 --dry   # 只统计不删除
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import AuditLog


class Command(BaseCommand):
    help = '删除超过保留期的审计日志'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7,
                            help='保留天数，默认 7')
        parser.add_argument('--dry', '--dry-run', action='store_true',
                            dest='dry', help='只统计，不实际删除')

    def handle(self, *args, **options):
        days = options['days']
        dry = options['dry']
        cutoff = timezone.now() - timedelta(days=days)

        qs = AuditLog.objects.filter(created_at__lt=cutoff)
        total = qs.count()

        if dry:
            self.stdout.write('%d 天前的审计日志 %d 条（试运行，未删除）'
                              % (days, total))
            return

        deleted = 0
        # 分批删除，避免一次性锁住大表
        while True:
            ids = list(qs.values_list('id', flat=True)[:1000])
            if not ids:
                break
            n, _ = AuditLog.objects.filter(id__in=ids).delete()
            deleted += n
            if len(ids) < 1000:
                break

        self.stdout.write('%d 天前的审计日志 %d 条，已删除 %d 条'
                          % (days, total, deleted))
