"""登录与登出信号，写审计日志。"""

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from apps.core.audit import log_action


@receiver(user_logged_in)
def on_user_login(sender, request, user, **kwargs):
    log_action(request, 'login', 'user', user.id)


@receiver(user_logged_out)
def on_user_logout(sender, request, user, **kwargs):
    if user is not None:
        log_action(request, 'logout', 'user', user.id)
