"""群体统计：按部门汇总异常情况，并对小样本分组做抑制。"""

from django.conf import settings
from django.db.models import Count

from apps.accounts.models import User
from apps.reports.models import Item


def min_group_size():
    return int(getattr(settings, 'MIN_GROUP_SIZE', 10))


def department_stats(tenant_id):
    """按部门统计员工数与异常人数。

    返回的每一行都带 suppressed 标记：人数低于阈值的分组不展示具体数字，
    只说明样本不足。这是防止通过小样本反推个体的硬约束。
    """
    threshold = min_group_size()

    departments = list(
        User.objects.filter(tenant_id=tenant_id, role=User.ROLE_EMPLOYEE,
                            deleted_at__isnull=True)
        .exclude(department='')
        .values_list('department', flat=True)
        .distinct()
        .order_by('department'))

    rows = []
    for dept in departments:
        members = User.objects.filter(
            tenant_id=tenant_id, role=User.ROLE_EMPLOYEE,
            department=dept, deleted_at__isnull=True)
        total = members.count()

        subject_ids = list(members.filter(subject__isnull=False)
                           .values_list('subject_id', flat=True))

        abnormal_subjects = 0
        if subject_ids:
            abnormal_subjects = (
                Item.objects
                .filter(tenant_id=tenant_id,
                        report__subject_id__in=subject_ids,
                        report__deleted_at__isnull=True,
                        flag='abnormal')
                .values('report__subject_id')
                .distinct()
                .count())

        suppressed = total < threshold
        rows.append({
            'department': dept,
            'total': None if suppressed else total,
            'abnormal': None if suppressed else abnormal_subjects,
            'rate': (None if suppressed or total == 0
                     else round(abnormal_subjects * 100.0 / total, 1)),
            'suppressed': suppressed,
        })

    return rows


def overall_stats(tenant_id):
    """全体汇总。只在总人数达到阈值时给出数字。"""
    threshold = min_group_size()
    members = User.objects.filter(tenant_id=tenant_id,
                                  role=User.ROLE_EMPLOYEE,
                                  deleted_at__isnull=True)
    total = members.count()

    if total < threshold:
        return {'total': None, 'abnormal': None, 'rate': None,
                'suppressed': True}

    subject_ids = list(members.filter(subject__isnull=False)
                       .values_list('subject_id', flat=True))
    abnormal = 0
    if subject_ids:
        abnormal = (Item.objects
                    .filter(tenant_id=tenant_id,
                            report__subject_id__in=subject_ids,
                            report__deleted_at__isnull=True,
                            flag='abnormal')
                    .values('report__subject_id')
                    .distinct()
                    .count())

    return {'total': total, 'abnormal': abnormal,
            'rate': round(abnormal * 100.0 / total, 1) if total else None,
            'suppressed': False}
