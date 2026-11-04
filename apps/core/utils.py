"""公共工具：假名化、文件名解析、图片校验。"""

import hashlib
import hmac
import io
import os
import re
import zipfile

from django.conf import settings
from PIL import Image

# 支持的图片格式，与 Pillow 能打开的常见类型一致
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'bmp', 'tif', 'tiff', 'webp'}
ALLOWED_FORMATS = {'JPEG', 'PNG', 'BMP', 'TIFF', 'WEBP', 'GIF'}

# 单个文件与解压总量的上限
MAX_FILE_MB = 20
MAX_TOTAL_MB = 500
# 压缩比上限，超过视为压缩炸弹
MAX_COMPRESS_RATIO = 200
# 单张图片的像素上限。体积小但像素极高的图片解码后会占用大量内存，
# 报告单正常在 1000 万像素以内，这里留足余量。
MAX_PIXELS = 80_000_000


def make_subject_key(employee_no):
    """由工号生成 HMAC 假名。盐值从环境变量读取，不入库。"""
    salt = os.environ.get('SUBJECT_SALT', '')
    if not salt:
        # 开发期允许空盐，生产必须配置
        salt = 'dev-salt-not-for-production'
    digest = hmac.new(salt.encode('utf-8'),
                      str(employee_no).encode('utf-8'),
                      hashlib.sha256).hexdigest()
    return digest[:24]


def parse_employee_no(filename):
    """从文件名解析工号。

    约定形如 `工号_姓名.jpg`，取第一个分隔符前的部分。
    纯工号（无分隔符）整体作为工号。
    解析不出返回 None，交由管理员手工匹配。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = stem.strip()
    if not stem:
        return None
    head = re.split(r'[_\-\s]', stem)[0]
    if not head or not re.match(r'^[A-Za-z0-9]+$', head):
        return None
    return head


def is_allowed_file(filename):
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in ALLOWED_EXTENSIONS


def validate_image(file_obj):
    """校验文件确实是可识别的图片，返回 (ok, 说明)。

    只看扩展名不够，用 Pillow 实际打开一次，确认不是伪装的文件。
    """
    file_obj.seek(0)
    try:
        with Image.open(file_obj) as img:
            fmt = img.format
            width, height = img.size
        file_obj.seek(0)
        if fmt not in ALLOWED_FORMATS:
            return False, '不支持的格式：%s' % fmt
        if width < 100 or height < 100:
            return False, '图片过小：%dx%d' % (width, height)
        if width * height > MAX_PIXELS:
            return False, '图片像素过多：%dx%d' % (width, height)
        return True, '%s %dx%d' % (fmt, width, height)
    except Exception as e:
        file_obj.seek(0)
        return False, '无法识别的图片：%s' % str(e)[:60]


def safe_extract_zip(zip_file, max_total_mb=MAX_TOTAL_MB,
                     max_ratio=MAX_COMPRESS_RATIO):
    """安全解压 ZIP，返回 [(文件名, 字节内容)]。

    拒绝路径穿越与压缩炸弹；只取图片，忽略目录结构。
    """
    results = []
    total_bytes = 0
    max_total = max_total_mb * 1024 * 1024

    try:
        with zipfile.ZipFile(zip_file) as zf:
            infos = zf.infolist()
            if not infos:
                return [], '压缩包为空'

            compressed = sum(i.compress_size for i in infos) or 1
            uncompressed = sum(i.file_size for i in infos)

            if compressed > 0 and uncompressed / compressed > max_ratio:
                return [], '压缩比异常，疑似压缩炸弹'
            if uncompressed > max_total:
                return [], '解压后超过 %d MB 上限' % max_total_mb

            for info in infos:
                if info.is_dir():
                    continue
                name = os.path.basename(info.filename)
                if not name or '..' in info.filename:
                    continue
                if not is_allowed_file(name):
                    continue
                data = zf.read(info)
                total_bytes += len(data)
                if total_bytes > max_total:
                    return [], '解压后超过 %d MB 上限' % max_total_mb
                results.append((name, data))
    except zipfile.BadZipFile:
        return [], '不是有效的 ZIP 文件'
    except Exception as e:
        return [], '解压失败：%s' % str(e)[:60]

    if not results:
        return [], '压缩包内没有可用的图片'
    return results, ''


def read_file_bytes(django_file, max_mb=MAX_FILE_MB):
    """读取 Django 上传文件的内容，超限返回空。"""
    if django_file.size > max_mb * 1024 * 1024:
        return b''
    django_file.seek(0)
    data = django_file.read()
    django_file.seek(0)
    return data


def open_image_stream(data):
    """把字节转为可交给 Pillow 与 Django 的文件对象。"""
    return io.BytesIO(data)


def name_key(name):
    """跨报告对齐用的指标名归一化，规则与 tools/34_store.py 保持一致。"""
    s = '' if name is None else str(name)
    s = s.strip().lower().replace(' ', '')
    for a, b in (
        ('：', ':'), ('－', '-'), ('—', '-'), ('–', '-'),
        ('~', '-'), ('～', '-'), ('（', '('), ('）', ')'),
    ):
        s = s.replace(a, b)
    s = re.sub(r'^[\*＊★☆#＃]+', '', s)
    return s


def _name_variants(nm):
    """项名在正文里的可能写法：原样，以及去掉末尾括号编码的形式。

    抽取层把名称与编码合成一个字段（"淋巴细胞绝对值(LYM#)"），
    而模型写正文时通常不带编码，只按原样匹配会漏掉长名。
    """
    out = [nm]
    base = re.sub(r'[（(][^）)]*[）)]\s*$', '', nm).strip()
    if base and base != nm:
        out.append(base)
    return out


def cite_items_of(body, raw_names):
    """找出正文提到的指标名，返回匹配到的原始名列表。

    规则与 tools/35_llm_explain.py 的 independent_names 一致：按出现位置判断，
    只有出现过一次不被更长名字覆盖，才算独立提到。
    单纯的字符串包含会把"红细胞 3.55"里的短名误删。
    """
    text = body or ''
    names = [n.strip() for n in raw_names if len((n or '').strip()) >= 2]
    if not names:
        return []

    pairs = [(v, nm) for nm in names for v in _name_variants(nm)]

    spans = {}
    for v, _nm in pairs:
        pos, i = [], text.find(v)
        while i >= 0:
            pos.append((i, i + len(v)))
            i = text.find(v, i + 1)
        if pos:
            spans.setdefault(v, []).extend(pos)

    out = []
    for nm in names:
        mine = [v for v, o in pairs if o == nm]
        others = [(v, o) for v, o in pairs if o != nm]
        keep = False
        for v in mine:
            for (a, b) in spans.get(v, []):
                covered = any(
                    a >= a2 and b <= b2
                    for v2, _o in others
                    for (a2, b2) in spans.get(v2, []))
                if not covered:
                    keep = True
                    break
            if keep:
                break
        if keep and nm not in out:
            out.append(nm)
    return out


NUM_RE = re.compile(r'^[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$')


def to_num(v):
    """把原始值文本转成数值，转不了返回 None。

    去掉升降箭头与不等号前缀，"<0.5" 这类只取数字部分。
    """
    s = '' if v is None else str(v).strip()
    if not s:
        return None
    s = re.sub(r'[↑↓]', '', s).lstrip('<>≤≥＜＞')
    if NUM_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return None
    return None
