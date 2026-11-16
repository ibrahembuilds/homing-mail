"""Configuration: environment for deployment wiring, database for things the
operator changes from the Settings page."""
import os
import re


def _bool(value, default=False):
    if value is None or value == '':
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


class Config:
    URL_PREFIX = '/campaigns'

    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.DATA_DIR = e.get('CAMPAIGNS_DATA', '/data')
        self.DB_PATH = e.get('CAMPAIGNS_DB') or os.path.join(self.DATA_DIR, 'campaigns.db')
        # The mail server's SECRET_KEY is reused when no dedicated one is given.
        self.SECRET_KEY = e.get('CAMPAIGNS_SECRET') or e.get('SECRET_KEY') or ''
        self.BRAND_NAME = e.get('BRAND_NAME') or e.get('SITENAME') or 'Homing'
        color = e.get('BRAND_COLOR') or e.get('LOGO_BACKGROUND') or ''
        # Used inside a <style> block, so only a plain hex colour is accepted.
        self.BRAND_COLOR = color if re.fullmatch(r'#[0-9a-fA-F]{3,8}', color) else '#5b3be8'
        self.BRAND_LOGO_URL = e.get('BRAND_LOGO_URL') or e.get('LOGO_URL') or ''
        self.BRAND_WEBSITE = e.get('WEBSITE', '')
        self.DOMAIN = e.get('DOMAIN', '')
        hostname = (e.get('HOSTNAMES', '').split(',')[0]).strip()
        self.HOSTNAME = hostname
        self.PUBLIC_URL = (e.get('PUBLIC_URL') or (f'https://{hostname}' if hostname else 'http://localhost:8025')).rstrip('/')
        # 'proxy'  : the web proxy has already checked the admin login
        #            and proves it with a shared secret header.
        # 'password': standalone login with CAMPAIGNS_ADMIN_PASSWORD.
        self.AUTH_MODE = e.get('CAMPAIGNS_AUTH', 'password')
        self.PROXY_SECRET = e.get('CAMPAIGNS_PROXY_SECRET', '')
        self.ADMIN_PASSWORD = e.get('CAMPAIGNS_ADMIN_PASSWORD', '')
        # The first owner: the mail server's first administrator, or "admin".
        initial = (f"{e.get('INITIAL_ADMIN_ACCOUNT', '')}@{e.get('INITIAL_ADMIN_DOMAIN', '')}"
                   if e.get('INITIAL_ADMIN_ACCOUNT') and e.get('INITIAL_ADMIN_DOMAIN') else '')
        self.OWNER_EMAIL = (e.get('CAMPAIGNS_OWNER') or initial or 'admin').strip().lower()
        self.WEB_ADMIN = e.get('WEB_ADMIN', '/admin')
        self.WEB_WEBMAIL = e.get('WEB_WEBMAIL', '/webmail')
        # Running next to the bundled mail server (links to its admin and webmail).
        self.MAIL_SERVER = _bool(e.get('CAMPAIGNS_MAIL_SERVER'), bool(hostname))
        # The mail server's user API, used to create mailboxes for new team members.
        self.MAIL_API_URL = e.get('CAMPAIGNS_MAIL_API', 'http://admin:8080/api/v1')
        self.MAIL_API_TOKEN = e.get('API_TOKEN', '') if _bool(e.get('API'), False) else ''
        # Mail plumbing defaults (service names on the compose network).
        self.SMTP_HOST = e.get('CAMPAIGNS_SMTP_HOST', 'smtp')
        self.SMTP_PORT = int(e.get('CAMPAIGNS_SMTP_PORT', '25'))
        self.SMTP_USER = e.get('CAMPAIGNS_SMTP_USER', '')
        self.SMTP_PASSWORD = e.get('CAMPAIGNS_SMTP_PASSWORD', '')
        self.SMTP_TLS = e.get('CAMPAIGNS_SMTP_TLS', 'none')  # none | starttls | ssl
        self.RSPAMD_URL = e.get('CAMPAIGNS_RSPAMD_URL', 'http://antispam:11334')
        self.IMAP_HOST = e.get('CAMPAIGNS_IMAP_HOST', 'front')
        self.IMAP_PORT = int(e.get('CAMPAIGNS_IMAP_PORT', '993'))
        self.DKIM_SELECTOR = e.get('DKIM_SELECTOR', 'dkim')
        self.DKIM_PATH = e.get('DKIM_PATH', '/dkim/{domain}.{selector}.key')
        self.PUBLIC_IP = e.get('PUBLIC_IP', '').strip()
        # Initial values for the Settings page, applied once on first start.
        self.SEED = {
            'bounce_address': e.get('CAMPAIGNS_BOUNCE_ADDRESS', ''),
            'imap_user': e.get('CAMPAIGNS_IMAP_USER', ''),
            'imap_password': e.get('CAMPAIGNS_IMAP_PASSWORD', ''),
            'timezone': e.get('TZ', ''),
            'from_email': e.get('CAMPAIGNS_FROM_EMAIL', ''),
            'reply_imap_user': e.get('CAMPAIGNS_REPLY_USER', ''),
            'reply_imap_password': e.get('CAMPAIGNS_REPLY_PASSWORD', ''),
        }
        # Webhooks may point at this network (a self-hosted n8n, for example)
        # only when the operator says so; otherwise they could probe internal services.
        self.WEBHOOK_PRIVATE = _bool(e.get('CAMPAIGNS_WEBHOOK_PRIVATE'))
        self.SECURE_COOKIES = self.PUBLIC_URL.startswith('https://')
        self.TESTING = _bool(e.get('CAMPAIGNS_TESTING'))


# Operator-editable settings and their defaults. Everything is stored as text.
DEFAULT_SETTINGS = {
    # sender identity
    'from_name': '',
    'from_email': '',
    'reply_to': '',
    'postal_address': '',
    'email_style': 'branded',     # branded: header, card and footer | plain: like a personal email
    'email_signature': '',        # lines under every email: name, role, company, phone, website
    'email_logo_url': '',         # https:// image shown in the header of branded emails
    'timezone': 'UTC',
    # pacing defaults for new campaigns
    'rate_per_hour': '120',
    'delay_min': '',
    'delay_max': '',
    'window_start': '',
    'window_end': '',
    'window_days': '0123456',
    'daily_cap': '0',
    'domain_hourly_cap': '0',
    # warm-up: a server-wide daily ceiling that grows every day
    'warmup_enabled': '0',
    'warmup_start': '50',
    'warmup_growth': '1.5',
    'warmup_max': '5000',
    'warmup_started_at': '',
    # tracking
    'track_opens': '1',
    'track_clicks': '1',
    # AI agent
    'ai_provider': 'openrouter',
    'ai_key_openrouter': '',
    'ai_key_anthropic': '',
    'ai_model_openrouter': '',
    'ai_model_anthropic': '',
    'ai_require_approval': '1',
    'ai_brand_voice': '',
    'ai_model_writer': '',       # model for per-lead emails and the inbox; empty = the agent's model
    'ai_web_search': '1',        # the agent may search the web
    'ai_reply_mode': 'off',      # reply autopilot: off, draft or send (see replies.auto_answer)
    # replies: the mailbox leads answer to (read-only)
    'reply_imap_user': '',
    'reply_imap_password': '',
    'reply_imap_host': '',
    'reply_imap_port': '',
    # lead tools and domain setup
    'hunter_api_key': '',
    'cloudflare_token': '',
    'calendar_token': '',
    # bounce / complaint mailbox
    'bounce_address': '',
    'imap_host': '',
    'imap_port': '',
    'imap_user': '',
    'imap_password': '',
    'imap_verify_tls': '0',
    # SMTP override (empty = use environment defaults)
    'smtp_host': '',
    'smtp_port': '',
    'smtp_user': '',
    'smtp_password': '',
    'smtp_tls': '',
}

SECRET_SETTINGS = ('ai_key_openrouter', 'ai_key_anthropic', 'imap_password', 'smtp_password',
                   'reply_imap_password', 'hunter_api_key', 'cloudflare_token')
