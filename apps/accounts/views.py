from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.models import Consent
from apps.core.audit import log_action

CONSENT_VERSION = 'v1'


@login_required
def consent_view(request):
    """知情同意书签署页。已签署的用户访问会直接跳回目标页。"""
    next_url = request.GET.get('next') or request.POST.get('next') or '/reports/'

    already = Consent.objects.filter(
        user_id=request.user.id,
        consent_type='processing',
        revoked_at__isnull=True,
    ).exists()

    if request.method == 'POST':
        if not already:
            Consent.objects.create(
                tenant_id=request.user.tenant_id,
                user=request.user,
                consent_type='processing',
                consent_version=CONSENT_VERSION,
                granted_at=timezone.now(),
            )
            log_action(request, 'consent', 'consent', request.user.id,
                       '版本 %s' % CONSENT_VERSION)
            messages.success(request, '已完成知情同意签署')
        return redirect(next_url)

    return render(request, 'accounts/consent.html', {
        'next': next_url,
        'already': already,
        'version': CONSENT_VERSION,
    })


@login_required
def revoke_consent(request):
    """撤回知情同意。

    保留原记录只写撤回时间，撤回记录本身就是授权链的一部分。
    撤回后中间件会重新拦截员工访问报告页，再次签署即可恢复。
    """
    if request.method != 'POST':
        return redirect('accounts:consent')

    updated = Consent.objects.filter(
        user_id=request.user.id,
        consent_type='processing',
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())

    if updated:
        log_action(request, 'revoke', 'consent', request.user.id,
                   '版本 %s' % CONSENT_VERSION)
        messages.success(
            request, '已撤回授权。撤回后系统不再处理您的新报告。')
    else:
        messages.info(request, '当前没有可撤回的授权')

    return redirect('accounts:consent')


@login_required
def password_change(request):
    """修改自己的密码。改完保持登录状态。"""
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            # 不刷新 session 的话，改完密码会被登出
            update_session_auth_hash(request, form.user)
            log_action(request, 'password', 'user', request.user.id)
            messages.success(request, '密码已更新')
            return redirect('reports:report_list')
    else:
        form = PasswordChangeForm(request.user)

    return render(request, 'accounts/password_change.html', {'form': form})
