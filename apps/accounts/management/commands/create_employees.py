"""按报告里的工号批量创建员工账号。

上传报告后逐个在后台建账号很费时，这个命令按报告号补齐账号并绑定体检人。
"""

import secrets

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.core.models import Organization
from apps.reports.models import Report
from apps.reports.services import looks_like_generic_name


class Command(BaseCommand):
    help = '为已有报告批量创建员工账号，并绑定对应的体检人'

    def add_arguments(self, parser):
        parser.add_argument('--org', default='default', help='组织编码')
        parser.add_argument('--password', default='',
                            help='统一初始密码，省略则随机生成')
        parser.add_argument('--dry-run', action='store_true',
                            help='只列出将要创建的账号')

    def handle(self, *args, **options):
        org = Organization.objects.filter(code=options['org']).first()
        if org is None:
            raise CommandError('组织不存在：%s' % options['org'])

        rows = (Report.objects
                .filter(tenant_id=org.id, deleted_at__isnull=True,
                        subject__isnull=False)
                .values_list('report_no', 'subject_id')
                .order_by('report_no')
                .distinct())

        subject_of = {}
        for report_no, subject_id in rows:
            subject_of.setdefault(report_no, subject_id)

        generic = sorted(n for n in subject_of if looks_like_generic_name(n))
        existing = set(User.objects.filter(tenant=org)
                       .values_list('username', flat=True))
        todo = [(n, s) for n, s in subject_of.items()
                if n not in existing and not looks_like_generic_name(n)]

        if options['dry_run']:
            self.stdout.write('将创建 %d 个账号' % len(todo))
            for username, _ in todo:
                self.stdout.write('  %s' % username)
            if generic:
                self.stdout.write('跳过 %d 个不像工号的名字：%s'
                                  % (len(generic), '、'.join(generic[:5])))
            return

        if todo:
            password = options['password'] or secrets.token_urlsafe(9)
            for username, subject_id in todo:
                User.objects.create_user(
                    username=username, password=password, tenant=org,
                    role=User.ROLE_EMPLOYEE, subject_id=subject_id)
            self.stdout.write(self.style.SUCCESS(
                '创建 %d 个账号，初始密码 %s' % (len(todo), password)))
        else:
            self.stdout.write('没有需要创建的账号')

        if generic:
            self.stdout.write(self.style.WARNING(
                '跳过 %d 个不像工号的名字：%s'
                % (len(generic), '、'.join(generic[:5]))))
