"""员工账号批量创建的回归测试。"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import User
from apps.core.models import Organization
from apps.reports.models import Report, Subject


class CreateEmployeesTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name='测试企业', code='t9')
        self.sub1 = Subject.objects.create(
            tenant_id=self.org.id, subject_key='k1', sex='')
        self.sub2 = Subject.objects.create(
            tenant_id=self.org.id, subject_key='k2', sex='')
        Report.objects.create(tenant_id=self.org.id, subject=self.sub1,
                              report_no='E001', schema_version='1')
        Report.objects.create(tenant_id=self.org.id, subject=self.sub2,
                              report_no='E002', schema_version='1')

    def test_creates_accounts_bound_to_subject(self):
        call_command('create_employees', org='t9', password='pass12345',
                     stdout=StringIO())

        user = User.objects.get(tenant=self.org, username='E001')
        self.assertEqual(user.role, User.ROLE_EMPLOYEE)
        self.assertEqual(user.subject_id, self.sub1.id)
        self.assertTrue(user.check_password('pass12345'))
        self.assertTrue(User.objects.filter(
            tenant=self.org, username='E002').exists())

    def test_skips_generic_names(self):
        Report.objects.create(tenant_id=self.org.id, subject=self.sub1,
                              report_no='TempDragFile_20261022_094238',
                              schema_version='1')
        call_command('create_employees', org='t9', password='pass12345',
                     stdout=StringIO())

        self.assertFalse(User.objects.filter(
            username__startswith='TempDragFile').exists())

    def test_does_not_duplicate_existing(self):
        call_command('create_employees', org='t9', password='pass12345',
                     stdout=StringIO())
        call_command('create_employees', org='t9', password='pass12345',
                     stdout=StringIO())

        self.assertEqual(
            User.objects.filter(tenant=self.org, username='E001').count(), 1)

    def test_dry_run_creates_nothing(self):
        out = StringIO()
        call_command('create_employees', org='t9', dry_run=True, stdout=out)

        self.assertEqual(
            User.objects.filter(tenant=self.org, role='employee').count(), 0)
        self.assertIn('E001', out.getvalue())

    def test_generates_password_when_omitted(self):
        call_command('create_employees', org='t9', stdout=StringIO())

        user = User.objects.get(tenant=self.org, username='E001')
        self.assertTrue(user.has_usable_password())
