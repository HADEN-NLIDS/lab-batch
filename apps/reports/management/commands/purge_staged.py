"""清理超时未确认的暂存文件。

上传分两步走，用户可能在看确认清单前就关掉页面。暂存的图片会一直占着
media 目录，这个命令删除超过保留期的暂存记录与对应文件。
"""

import os
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.reports.models import PendingFile

KEEP_HOURS = 24


class Command(BaseCommand):
    help = '清理超过保留期仍未确认的暂存文件'

    def add_arguments(self, parser):
        parser.add_argument('--hours', type=int, default=KEEP_HOURS,
                            help='保留小时数，默认 %d' % KEEP_HOURS)
        parser.add_argument('--dry-run', action='store_true',
                            help='只列出将要删除的文件')

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(hours=options['hours'])
        rows = list(PendingFile.objects.filter(created_at__lt=cutoff))
        if not rows:
            self.stdout.write('没有需要清理的暂存文件')
            return

        media_root = os.path.abspath(str(settings.MEDIA_ROOT))
        removed = 0

        for pending in rows:
            if options['dry_run']:
                self.stdout.write('将删除 %s（%s）'
                                  % (pending.filename, pending.state))
                continue

            abs_path = os.path.abspath(os.path.join(
                media_root, pending.image_path))
            # 只删 media 之内的文件，越界的一律跳过
            if abs_path.startswith(media_root + os.sep) and \
                    os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                    removed += 1
                except OSError:
                    pass
            pending.delete()

        if options['dry_run']:
            self.stdout.write('待清理 %d 条暂存记录（试运行）' % len(rows))
        else:
            self.stdout.write(self.style.SUCCESS(
                '清理 %d 条暂存记录，删除 %d 个文件' % (len(rows), removed)))
