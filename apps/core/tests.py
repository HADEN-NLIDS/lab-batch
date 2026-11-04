"""公共工具：图片校验、压缩包解压、假名化。"""

import io
import zipfile

from django.test import TestCase
from PIL import Image

from apps.core.utils import make_subject_key, safe_extract_zip, validate_image


def image_bytes(size=(400, 300), fmt='PNG', color='white'):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, fmt)
    return buf.getvalue()


class ValidateImageTest(TestCase):

    def test_accepts_normal_image(self):
        ok, msg = validate_image(io.BytesIO(image_bytes()))
        self.assertTrue(ok, msg)

    def test_rejects_non_image(self):
        ok, _ = validate_image(io.BytesIO(b'not an image'))
        self.assertFalse(ok)

    def test_rejects_too_small(self):
        ok, msg = validate_image(io.BytesIO(image_bytes((50, 50))))
        self.assertFalse(ok)
        self.assertIn('过小', msg)

    def test_rejects_excessive_pixels(self):
        buf = io.BytesIO()
        Image.new('1', (10000, 9000)).save(buf, 'PNG')
        ok, msg = validate_image(io.BytesIO(buf.getvalue()))
        self.assertFalse(ok)
        self.assertIn('像素', msg)


class SafeExtractZipTest(TestCase):

    def _zip(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        buf.seek(0)
        return buf

    def test_extracts_image_and_strips_directory(self):
        z = self._zip([('sub/E001_a.png', image_bytes())])
        entries, err = safe_extract_zip(z)

        self.assertEqual(err, '')
        self.assertEqual(entries[0][0], 'E001_a.png')

    def test_rejects_path_traversal(self):
        z = self._zip([('../../evil.png', image_bytes())])
        entries, _ = safe_extract_zip(z)

        self.assertEqual(entries, [])

    def test_ignores_non_image_entries(self):
        z = self._zip([('readme.txt', b'hello')])
        entries, err = safe_extract_zip(z)

        self.assertEqual(entries, [])
        self.assertIn('没有可用', err)

    def test_rejects_suspicious_ratio(self):
        z = self._zip([('big.png', b'\0' * (5 * 1024 * 1024))])
        entries, err = safe_extract_zip(z, max_ratio=2)

        self.assertEqual(entries, [])
        self.assertIn('压缩比', err)

    def test_rejects_broken_zip(self):
        entries, err = safe_extract_zip(io.BytesIO(b'not a zip'))

        self.assertEqual(entries, [])
        self.assertIn('ZIP', err)


class SubjectKeyTest(TestCase):

    def test_same_input_gives_same_key(self):
        self.assertEqual(make_subject_key('E001'), make_subject_key('E001'))

    def test_different_input_gives_different_key(self):
        self.assertNotEqual(make_subject_key('E001'), make_subject_key('E002'))

    def test_key_is_truncated(self):
        self.assertEqual(len(make_subject_key('E001')), 24)
