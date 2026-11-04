from django.contrib.auth.models import (AbstractBaseUser, BaseUserManager,
                                        PermissionsMixin)
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    """自定义用户模型的管理器。Django 的 createsuperuser 依赖它。"""

    def create_user(self, username, password=None, **extra_fields):
        if not username:
            raise ValueError('用户名不能为空')
        if not extra_fields.get('tenant'):
            raise ValueError('必须指定组织')
        user = self.model(username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, password=None, **extra_fields):
        extra_fields.setdefault('role', User.ROLE_ADMIN)
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if not extra_fields.get('tenant'):
            # 超级用户首次创建时若未提供组织，取第一个或新建一个默认组织
            from apps.core.models import Organization
            org = Organization.objects.first()
            if org is None:
                org = Organization.objects.create(name='默认组织', code='default')
            extra_fields['tenant'] = org
        return self.create_user(username, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """用户账号，三级角色。以 tenant 加 username 唯一。"""

    ROLE_EMPLOYEE = 'employee'
    ROLE_MANAGER = 'manager'
    ROLE_ADMIN = 'admin'
    ROLE_CHOICES = [
        (ROLE_EMPLOYEE, '受检员工'),
        (ROLE_MANAGER, '企业管理层'),
        (ROLE_ADMIN, '运维人员'),
    ]

    tenant = models.ForeignKey('core.Organization', on_delete=models.PROTECT,
                               db_column='tenant_id', related_name='users')
    username = models.CharField(max_length=64)
    display_name = models.CharField(max_length=64, blank=True, default='')
    role = models.CharField(max_length=16, choices=ROLE_CHOICES,
                            default=ROLE_EMPLOYEE)
    department = models.CharField(max_length=64, blank=True, default='')
    subject = models.ForeignKey('reports.Subject', null=True, blank=True,
                                on_delete=models.SET_NULL,
                                db_column='subject_id', related_name='users')
    status = models.SmallIntegerField(default=1)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = 'app_user'
        verbose_name = '用户'
        verbose_name_plural = '用户'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'username'],
                                    name='uk_tenant_username'),
        ]
        indexes = [
            models.Index(fields=['tenant', 'role'], name='idx_tenant_role'),
            models.Index(fields=['tenant', 'department'],
                         name='idx_tenant_dept'),
        ]

    def __str__(self):
        return self.username

    @property
    def is_employee(self):
        return self.role == self.ROLE_EMPLOYEE

    @property
    def is_manager(self):
        return self.role == self.ROLE_MANAGER

    @property
    def is_admin_role(self):
        return self.role == self.ROLE_ADMIN


class Consent(models.Model):
    """知情同意授权链。保留撤销记录，不删除行。"""

    TYPE_CHOICES = [
        ('processing', '报告处理'),
        ('share', '数据共享'),
    ]

    tenant_id = models.BigIntegerField()
    user = models.ForeignKey('accounts.User', on_delete=models.CASCADE,
                             db_column='user_id', related_name='consents')
    consent_type = models.CharField(max_length=32, choices=TYPE_CHOICES,
                                    default='processing')
    consent_version = models.CharField(max_length=16, blank=True, default='')
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'consent'
        verbose_name = '知情同意'
        verbose_name_plural = '知情同意'
        indexes = [
            models.Index(fields=['tenant_id', 'user'],
                         name='idx_tenant_user'),
            models.Index(fields=['user', 'consent_type'],
                         name='idx_user_type'),
        ]

    def __str__(self):
        return '%s %s' % (self.user_id, self.consent_type)

    @property
    def is_active(self):
        return self.revoked_at is None
