"""知情同意拦截：受检员工未签署时不能访问报告。"""

from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin

from apps.accounts.models import Consent

# 这些路径不需要同意，否则会无限重定向或拦住登录与管理后台
EXEMPT_PREFIXES = (
    '/login', '/logout', '/consent', '/admin',
    '/static', '/media', '/favicon.ico',
)


class ConsentMiddleware(MiddlewareMixin):
    """只对受检员工生效。

    管理层看的是聚合数据，运维负责系统本身，都不属于被处理健康数据的对象，
    因此不要求他们签署同意书。
    """

    def process_request(self, request):
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return None
        if user.role != 'employee':
            return None

        path = request.path or '/'
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return None

        signed = Consent.objects.filter(
            user_id=user.id,
            consent_type='processing',
            revoked_at__isnull=True,
        ).exists()
        if signed:
            return None

        return redirect('/consent/?next=%s' % path)
