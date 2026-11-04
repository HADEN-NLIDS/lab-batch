"""任务 worker：循环取任务并执行。

用法：
    python manage.py run_worker            # 常驻
    python manage.py run_worker --once     # 处理一个任务后退出
    python manage.py run_worker --sleep 5  # 空队列时的轮询间隔
"""

import os
import socket
import time

from django.core.management.base import BaseCommand

from apps.pipeline import services
from apps.pipeline.models import Task


class Command(BaseCommand):
    help = '启动任务 worker，从队列取任务并执行'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true',
                            help='只处理一个任务就退出')
        parser.add_argument('--sleep', type=int, default=3,
                            help='队列为空时的等待秒数')
        parser.add_argument('--worker-id', default='',
                            help='worker 标识，默认用主机名加进程号')

    def handle(self, *args, **options):
        worker_id = options['worker_id'] or '%s-%d' % (
            socket.gethostname(), os.getpid())
        once = options['once']
        sleep_sec = options['sleep']

        self.stdout.write('worker %s 已启动' % worker_id)

        while True:
            task = services.pick_task(worker_id)
            if task is None:
                if once:
                    self.stdout.write('队列为空，退出')
                    return
                time.sleep(sleep_sec)
                continue

            self.stdout.write('取到任务 #%d %s 报告 %s'
                              % (task.id, task.task_type, task.report_id))
            try:
                ok, code, msg = services.process(task)
            except Exception as e:
                ok, code, msg = False, 'exception', str(e)[:500]

            if ok:
                services.mark_done(task)
                self.stdout.write(self.style.SUCCESS(
                    '任务 #%d 完成' % task.id))
            else:
                services.mark_failed(task, code, msg)
                task.refresh_from_db()
                self.stdout.write(self.style.WARNING(
                    '任务 #%d 失败：%s，当前状态 %s'
                    % (task.id, msg[:120], task.status)))

            if once:
                return
