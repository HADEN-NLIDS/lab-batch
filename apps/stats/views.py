from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from apps.stats.services import department_stats, min_group_size, overall_stats


@login_required
def dashboard(request):
    """群体统计页。仅管理层与运维可访问。"""
    if request.user.role not in ('manager', 'admin'):
        raise PermissionDenied('无权查看群体统计')
    if request.tenant is None:
        raise PermissionDenied('账号未绑定组织')

    tenant_id = request.tenant.id
    return render(request, 'stats/dashboard.html', {
        'rows': department_stats(tenant_id),
        'overall': overall_stats(tenant_id),
        'threshold': min_group_size(),
    })
