"""知情同意拦截的测试。"""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Consent, User
from apps.core.models import Organization
from apps.reports.models import Report, Subject


class ConsentGateTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.subject = Subject.objects.create(tenant_id=self.org.id,
                                              subject_key='sk_1')

        self.employee = User.objects.create_user(
            username='E001', password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE, subject=self.subject)
        self.manager = User.objects.create_user(
            username='M001', password='x', tenant=self.org,
            role=User.ROLE_MANAGER)
        self.admin = User.objects.create_user(
            username='A001', password='x', tenant=self.org,
            role=User.ROLE_ADMIN)

        self.report = Report.objects.create(
            tenant_id=self.org.id, subject=self.subject,
            report_no='E001', schema_version='1')

    def test_employee_without_consent_is_redirected(self):
        self.client.force_login(self.employee)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/consent/', resp['Location'])

    def test_employee_with_consent_can_access(self):
        Consent.objects.create(tenant_id=self.org.id, user=self.employee,
                               consent_type='processing',
                               granted_at=timezone.now())
        self.client.force_login(self.employee)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 200)

    def test_manager_not_blocked(self):
        self.client.force_login(self.manager)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 200)

    def test_admin_not_blocked(self):
        self.client.force_login(self.admin)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 200)

    def test_consent_page_is_reachable_without_consent(self):
        self.client.force_login(self.employee)
        resp = self.client.get('/consent/')
        self.assertEqual(resp.status_code, 200)

    def test_signing_creates_consent_and_redirects(self):
        self.client.force_login(self.employee)
        resp = self.client.post('/consent/', {'next': '/reports/'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], '/reports/')

        self.assertTrue(Consent.objects.filter(
            user=self.employee, consent_type='processing',
            revoked_at__isnull=True).exists())

        # 签完后可以正常访问报告列表
        resp2 = self.client.get('/reports/')
        self.assertEqual(resp2.status_code, 200)

    def test_revoked_consent_blocks_again(self):
        c = Consent.objects.create(tenant_id=self.org.id, user=self.employee,
                                   consent_type='processing',
                                   granted_at=timezone.now())
        c.revoked_at = timezone.now()
        c.save(update_fields=['revoked_at'])

        self.client.force_login(self.employee)
        resp = self.client.get('/reports/')
        self.assertEqual(resp.status_code, 302)

    def test_signing_twice_does_not_duplicate(self):
        self.client.force_login(self.employee)
        self.client.post('/consent/', {'next': '/reports/'})
        self.client.post('/consent/', {'next': '/reports/'})
        self.assertEqual(Consent.objects.filter(
            user=self.employee, revoked_at__isnull=True).count(), 1)
