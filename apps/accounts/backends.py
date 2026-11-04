from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class TenantBackend(ModelBackend):
    """按组织编码加用户名认证。

    用户名只在租户内唯一，Django 默认后端按全局用户名查会取错人，
    因此自定义：先用 tenant_code 定位组织，再在该组织内匹配用户名。

    未提供 tenant_code 时，仅当系统只有一个启用组织才放行——
    这是为 Django admin 的默认登录表单留的兼容路径，多组织时必须显式指定。
    """

    def authenticate(self, request, username=None, password=None,
                     tenant_code=None, **kwargs):
        if not username or not password:
            return None

        UserModel = get_user_model()

        if not tenant_code:
            from apps.core.models import Organization
            orgs = list(Organization.objects.filter(status=1)[:2])
            if len(orgs) != 1:
                return None
            tenant_code = orgs[0].code

        try:
            user = UserModel._default_manager.get(
                tenant__code=tenant_code,
                username=username,
                deleted_at__isnull=True,
                is_active=True,
            )
        except UserModel.DoesNotExist:
            # 执行一次哈希运算，避免通过响应时间判断用户是否存在
            UserModel().set_password(password)
            return None

        if user.check_password(password):
            return user
        return None
