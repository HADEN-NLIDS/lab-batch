"""审计日志写入。

只记录关键操作，不追求全量。写入失败不阻断业务流程。审计是旁路，
不能因为它出错导致用户正在做的事失败。
"""

import logging

from apps.core.models import AuditLog

logger = logging.getLogger(__name__)


def client_ip(request):
    """取客户端 IP。有反向代理时优先用 X-Forwarded-For 的第一段。"""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR') or '')[:45]


def log_action(request, action, target_type='', target_id=None, detail=''):
    """写一条审计记录。没有租户上下文时跳过，登录前的匿名请求无法归属。"""
    user = getattr(request, 'user', None)
    tenant = getattr(request, 'tenant', None)
    if tenant is None and user is not None and getattr(
            user, 'is_authenticated', False):
        tenant = getattr(user, 'tenant', None)
    if tenant is None:
        return None

    try:
        return AuditLog.objects.create(
            tenant_id=tenant.id,
            user_id=(user.id if getattr(user, 'is_authenticated', False)
                     else None),
            action=action,
            target_type=str(target_type)[:32],
            target_id=target_id,
            detail=str(detail)[:512],
            ip=client_ip(request),
        )
    except Exception:
        logger.exception('审计日志写入失败: %s %s', action, target_type)
        return None
