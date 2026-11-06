"""修改密码的测试。"""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Consent, User
from apps.core.models import AuditLog, Organization


class PasswordChangeTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.user = User.objects.create_user(
            username='E001', password='oldpass123', tenant=self.org,
            role=User.ROLE_EMPLOYEE)
        Consent.objects.create(
            tenant_id=self.org.id, user=self.user,
            consent_type='processing', granted_at=timezone.now())

    def test_requires_login(self):
        resp = self.client.get('/password/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp['Location'])

    def test_page_renders(self):
        self.client.force_login(self.user)
        resp = self.client.get('/password/')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '修改密码')

    def test_change_succeeds(self):
        self.client.force_login(self.user)
        resp = self.client.post('/password/', {
            'old_password': 'oldpass123',
            'new_password1': 'newpass456',
            'new_password2': 'newpass456',
        })

        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newpass456'))

    def test_stays_logged_in(self):
        self.client.force_login(self.user)
        self.client.post('/password/', {
            'old_password': 'oldpass123',
            'new_password1': 'newpass456',
            'new_password2': 'newpass456',
        })

        # 改完密码还能正常访问报告页，没被登出
        self.assertEqual(self.client.get('/reports/').status_code, 200)

    def test_wrong_old_password_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post('/password/', {
            'old_password': 'wrongpass',
            'new_password1': 'newpass456',
            'new_password2': 'newpass456',
        })

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('oldpass123'))

    def test_pure_numeric_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post('/password/', {
            'old_password': 'oldpass123',
            'new_password1': '12345678',
            'new_password2': '12345678',
        })

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('oldpass123'))

    def test_short_password_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post('/password/', {
            'old_password': 'oldpass123',
            'new_password1': 'ab1',
            'new_password2': 'ab1',
        })

        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('oldpass123'))

    def test_writes_audit(self):
        self.client.force_login(self.user)
        self.client.post('/password/', {
            'old_password': 'oldpass123',
            'new_password1': 'newpass456',
            'new_password2': 'newpass456',
        })

        self.assertTrue(AuditLog.objects.filter(
            user_id=self.user.id, action='password').exists())
