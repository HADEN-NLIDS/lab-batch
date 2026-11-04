"""群体统计的测试：小样本抑制与角色权限。"""

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Consent, User
from apps.core.models import Organization
from apps.reports.models import Item, Report, Subject
from apps.stats.services import department_stats, overall_stats


class DepartmentStatsTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t1')

    def _make_employee(self, username, dept, with_abnormal=False):
        subject = Subject.objects.create(
            tenant_id=self.org.id, subject_key='sk_%s' % username)
        user = User.objects.create_user(
            username=username, password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE, department=dept, subject=subject)

        report = Report.objects.create(
            tenant_id=self.org.id, subject=subject,
            report_no='R_%s' % username, schema_version='1')
        Item.objects.create(
            tenant_id=self.org.id, report=report, seq=0,
            name_raw='白细胞计数', name_key='白细胞计数',
            flag='abnormal' if with_abnormal else 'normal')
        return user

    def test_department_above_threshold_shows_numbers(self):
        for i in range(10):
            self._make_employee('A%03d' % i, '技术部',
                                with_abnormal=(i < 3))

        rows = department_stats(self.org.id)
        row = next(r for r in rows if r['department'] == '技术部')
        self.assertFalse(row['suppressed'])
        self.assertEqual(row['total'], 10)
        self.assertEqual(row['abnormal'], 3)
        self.assertEqual(row['rate'], 30.0)

    def test_department_below_threshold_suppressed(self):
        for i in range(9):
            self._make_employee('B%03d' % i, '市场部', with_abnormal=True)

        rows = department_stats(self.org.id)
        row = next(r for r in rows if r['department'] == '市场部')
        self.assertTrue(row['suppressed'])
        self.assertIsNone(row['total'])
        self.assertIsNone(row['abnormal'])
        self.assertIsNone(row['rate'])

    def test_departments_are_separated(self):
        for i in range(10):
            self._make_employee('C%03d' % i, '技术部', with_abnormal=True)
        for i in range(3):
            self._make_employee('D%03d' % i, '行政部', with_abnormal=False)

        rows = {r['department']: r for r in department_stats(self.org.id)}
        self.assertFalse(rows['技术部']['suppressed'])
        self.assertTrue(rows['行政部']['suppressed'])

    def test_employee_without_department_ignored(self):
        self._make_employee('E001', '', with_abnormal=True)
        self.assertEqual(department_stats(self.org.id), [])

    def test_overall_below_threshold(self):
        for i in range(5):
            self._make_employee('F%03d' % i, '财务部', with_abnormal=True)
        overall = overall_stats(self.org.id)
        self.assertTrue(overall['suppressed'])
        self.assertIsNone(overall['total'])

    def test_overall_above_threshold(self):
        for i in range(12):
            self._make_employee('G%03d' % i, '财务部',
                                with_abnormal=(i < 6))
        overall = overall_stats(self.org.id)
        self.assertFalse(overall['suppressed'])
        self.assertEqual(overall['total'], 12)
        self.assertEqual(overall['abnormal'], 6)


class StatsViewTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t1')
        self.manager = User.objects.create_user(
            username='M1', password='x', tenant=self.org,
            role=User.ROLE_MANAGER)
        self.employee = User.objects.create_user(
            username='E1', password='x', tenant=self.org,
            role=User.ROLE_EMPLOYEE)
        Consent.objects.create(tenant_id=self.org.id, user=self.employee,
                               consent_type='processing',
                               granted_at=timezone.now())

    def test_manager_can_view(self):
        self.client.force_login(self.manager)
        resp = self.client.get('/stats/')
        self.assertEqual(resp.status_code, 200)

    def test_employee_forbidden(self):
        self.client.force_login(self.employee)
        resp = self.client.get('/stats/')
        self.assertEqual(resp.status_code, 403)

    def test_suppression_message_shown(self):
        """样本不足时页面应说明原因，而不是显示数字。"""
        for i in range(3):
            Subject.objects.create(tenant_id=self.org.id,
                                   subject_key='sk_%d' % i)
            User.objects.create_user(
                username='S%03d' % i, password='x', tenant=self.org,
                role=User.ROLE_EMPLOYEE, department='小部门')

        self.client.force_login(self.manager)
        resp = self.client.get('/stats/')
        body = resp.content.decode('utf-8')
        self.assertIn('样本不足', body)
        self.assertIn('小部门', body)
