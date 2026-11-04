"""清理软删除超期的报告原图。

用法：
    python manage.py purge_images --days 7          # 实际执行
    python manage.py purge_images --days 7 --dry   # 只统计不删除
"""

import os
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.reports.models import Report, ReportPage
from apps.reports.signals import remove_image_file


class Command(BaseCommand):
    help = '删除已软删除超过指定天数的报告原图，记录保留'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7,
                            help='软删除后保留天数，默认 7')
        parser.add_argument('--dry', action='store_true',
                            help='只统计，不实际删除')

    def handle(self, *args, **options):
        days = options['days']
        dry = options['dry']
        cutoff = timezone.now() - timedelta(days=days)

        reports = Report.objects.filter(
            deleted_at__isnull=False, deleted_at__lt=cutoff)
        pages = ReportPage.objects.filter(
            report__in=reports).exclude(image_path='')

        total = pages.count()
        removed = 0
        for page in pages:
            if dry:
                removed += 1
                continue
            if remove_image_file(page.image_path):
                removed += 1
            page.image_path = ''
            page.save(update_fields=['image_path'])

        scope = '（试运行，未改动）' if dry else ''
        self.stdout.write(
            '%d 天前软删除的报告 %d 份，待清理页图 %d 张，处理 %d 张%s'
            % (days, reports.count(), total, removed, scope))
