"""Django settings for lab-batch.

数据库密码等凭据从 .env 或环境变量读取，不写入本文件。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY', 'dev-only-key-not-for-production')
DEBUG = os.environ.get('DJANGO_DEBUG', '1') == '1'
ALLOWED_HOSTS = [h for h in os.environ.get(
    'DJANGO_ALLOWED_HOSTS', '*').split(',') if h]

# 明示 PyMySQL 作为数据库驱动，避免在 Windows 上编译 mysqlclient
import pymysql  # noqa: E402

pymysql.install_as_MySQLdb()

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'apps.core',
    'apps.accounts',
    'apps.reports',
    'apps.pipeline',
    'apps.stats',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.TenantMiddleware',
    'apps.accounts.middleware.ConsentMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('DB_NAME', 'lab_batch'),
        'USER': os.environ.get('DB_USER', 'root'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DB_PORT', '3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

AUTH_USER_MODEL = 'accounts.User'

# 用户名只在租户内唯一，认证需要带组织编码，因此不用默认后端
AUTHENTICATION_BACKENDS = [
    'apps.accounts.backends.TenantBackend',
]

# auth.E003 要求 USERNAME_FIELD 全局唯一。本项目的用户名校验由
# unique_together(tenant, username) 保证，因此关闭这条静态检查。
# 用户名在租户内唯一而非全局唯一，由 TenantBackend 负责消歧
SILENCED_SYSTEM_CHECKS = ['auth.E003', 'auth.W004']

AUTH_PASSWORD_VALIDATORS = [
    # 只保留长度与纯数字两条。相似性与常见密码字典校验对内部账号收益不大，
    # 却会逼出更随意、更难记的密码。
    {'NAME': 'django.contrib.auth.password_validation.'
             'MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.'
             'NumericPasswordValidator'},
]

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static'] if (BASE_DIR / 'static').exists() else []

MEDIA_URL = '/media/'
# 环境变量为空字符串时回落到默认目录，避免 MEDIA_ROOT 变成当前目录
MEDIA_ROOT = Path(os.environ.get('MEDIA_ROOT') or (BASE_DIR / 'media'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login'

# 上传限制：批量场景需要放宽，单文件不宜过大
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

SESSION_COOKIE_AGE = 8 * 60 * 60

# 群体统计的最小分组样本量，低于该值不展示
MIN_GROUP_SIZE = int(os.environ.get('MIN_GROUP_SIZE', '10'))
