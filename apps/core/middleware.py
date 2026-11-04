from django.utils.deprecation import MiddlewareMixin


class TenantMiddleware(MiddlewareMixin):
    """把当前登录用户所属的租户注入 request，供后续视图统一取用。

    只做注入，不做拦截。拦截放在视图层，因为登录页等公开页面不需要租户。
    """

    def process_request(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            request.tenant = getattr(user, 'tenant', None)
        else:
            request.tenant = None
        return None
