from django.contrib import messages
from django.contrib.auth.decorators import login_required
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
