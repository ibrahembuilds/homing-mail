"""Turns a campaign or API payload into the exact MIME message that is sent:
merge tags, unsubscribe link, tracked links, open pixel, required headers."""
import email.policy
import html as html_lib
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from . import security
from .db import loads

TAG_RE = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*([^{}]*?)\s*)?\}\}')
HREF_RE = re.compile(r'(<a\b[^>]*?\bhref\s*=\s*)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)
UNSUB_TAG_RE = re.compile(r'\{\{\s*unsubscribe_url\s*(\|[^{}]*)?\}\}', re.IGNORECASE)


def from_header(name, address):
    """A From value whose display name cannot pass for a different address.
    Folding a long header drops the quotes around the name, so characters that
    mean something in an address are taken out instead of relied on being quoted."""
    name = ' '.join(re.sub(r'[\x00-\x1f\x7f<>"@,;:()\[\]\\]+', ' ', str(name or '')).split())[:78]
    return formataddr((name, address)) if name else address


def safe_links(html):
    """Links that would run code (javascript:, vbscript:, data:) become '#'.
    A lead's website field merged into href="{{website}}" could otherwise carry one."""
    def replace(match):
        url = html_lib.unescape(match.group(3))
        scheme = re.sub(r'[\s\x00-\x1f]+', '', url).split(':', 1)[0].lower() if ':' in url else ''
        if scheme in ('javascript', 'vbscript', 'data'):
            return f'{match.group(1)}{match.group(2)}#{match.group(2)}'
        return match.group(0)
    return HREF_RE.sub(replace, html or '')


def merge(template, values, escape=False):
    """Replaces {{ name }} and {{ name|fallback }} tags."""
    def replace(match):
        value = values.get(match.group(1).lower())
        if value is None or value == '':
            value = match.group(2) or ''
        value = str(value)
        return html_lib.escape(value) if escape else value
    return TAG_RE.sub(replace, template or '')


def html_to_text(html):
    text = re.sub(r'(?is)<(script|style|head)\b.*?</\1>', '', html or '')
    text = re.sub(r'(?is)<a\b[^>]*?href\s*=\s*["\'](.*?)["\'][^>]*>(.*?)</a>',
                  lambda m: _link_text(m.group(2), m.group(1)), text)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|h[1-6]|li|tr|table|blockquote)>', '\n\n', text)
    text = re.sub(r'(?i)<li\b[^>]*>', '- ', text)
    text = re.sub(r'(?s)<[^>]+>', '', text)
    text = html_lib.unescape(text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r' ?\n ?', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _link_text(label, url):
    label = re.sub(r'(?s)<[^>]+>', '', label).strip()
    url = html_lib.unescape(url).strip()
    if not url or url.startswith('#'):
        return label
    if not label or label == url:
        return url
    return f'{label} ({url})'


def contact_values(contact):
    values = {}
    if contact is not None:
        for key, value in loads(contact['fields']).items():
            values[str(key).lower()] = value
        values.update({
            'email': contact['email'],
            'first_name': contact['first_name'],
            'last_name': contact['last_name'],
            'name': (contact['first_name'] + ' ' + contact['last_name']).strip(),
        })
    return values


class Renderer:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self._links = {}

    # -- URLs --------------------------------------------------------------
    @property
    def base(self):
        return self.cfg.PUBLIC_URL + self.cfg.URL_PREFIX + '/t'

    def open_url(self, mid):
        return f'{self.base}/o/{mid}/{security.sign(self.cfg.SECRET_KEY, "o", mid)}.gif'

    def click_url(self, mid, link_id):
        return f'{self.base}/c/{mid}/{link_id}/{security.sign(self.cfg.SECRET_KEY, "c", mid, link_id)}'

    def unsubscribe_url(self, mid):
        return f'{self.base}/u/{mid}/{security.sign(self.cfg.SECRET_KEY, "u", mid)}'

    def link_id(self, campaign_id, url):
        key = (campaign_id or 0, url)
        if key not in self._links:
            self.db.run('INSERT OR IGNORE INTO links (campaign_id, url) VALUES (?, ?)', key[0], url)
            self._links[key] = self.db.val('SELECT id FROM links WHERE campaign_id = ? AND url = ?', key[0], url)
            if len(self._links) > 20000:
                self._links.clear()
        return self._links[key]

    # -- body transforms ---------------------------------------------------
    def track_links(self, html, mid, campaign_id, skip=()):
        def replace(match):
            url = html_lib.unescape(match.group(3)).strip()
            if not url.lower().startswith(('http://', 'https://')) or url in skip or url.startswith(self.base):
                return match.group(0)
            tracked = self.click_url(mid, self.link_id(campaign_id, url))
            return f'{match.group(1)}{match.group(2)}{html_lib.escape(tracked)}{match.group(2)}'
        return HREF_RE.sub(replace, html)

    def add_pixel(self, html, mid):
        pixel = f'<img src="{self.open_url(mid)}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px">'
        return _insert_before_body_end(html, pixel)

    # -- campaign mail -----------------------------------------------------
    def campaign_bodies(self, campaign, contact, mid, settings, tracking=True, content=None, ai_line=None):
        """Returns (subject, html, text) for one recipient.

        content replaces the campaign's subject/html/text/preheader (a follow-up
        step, or an email the AI rewrote for this lead); ai_line fills {{ai_line}}.
        """
        source = content or campaign
        unsub = self.unsubscribe_url(mid)
        values = contact_values(contact)
        values['unsubscribe_url'] = unsub
        values['ai_line'] = ai_line or ''
        subject = ' '.join(merge(source['subject'], values).split())

        source_html = source['html'] or ''
        if not source_html.strip() and source['text']:
            source_html = '<p>' + html_lib.escape(source['text']).replace('\n\n', '</p><p>').replace('\n', '<br>') + '</p>'
        html = safe_links(merge(source_html, values, escape=True))
        if '<html' not in html.lower():
            html = ('<!doctype html><html><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
                    f'<body style="margin:0;padding:16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;'
                    f'line-height:1.5;color:#222">{html}</body></html>')
        if source['preheader']:
            pre = ('<div style="display:none;max-height:0;overflow:hidden;opacity:0">'
                   f'{html_lib.escape(merge(source["preheader"], values))}</div>')
            html = _insert_after_body_start(html, pre)
        address = (settings.get('postal_address') or '').strip()
        if not UNSUB_TAG_RE.search(source_html):
            footer = ('<div style="margin-top:32px;padding-top:12px;border-top:1px solid #ddd;'
                      'font-size:12px;line-height:1.5;color:#777">'
                      'Prefer not to get these emails? '
                      f'<a href="{html_lib.escape(unsub)}" style="color:#777">Unsubscribe</a>'
                      + (f'<br>{html_lib.escape(address)}' if address else '') + '</div>')
            html = _insert_before_body_end(html, footer)

        text_source = source['text'] or html_to_text(source_html)
        text = merge(text_source, values)
        if not UNSUB_TAG_RE.search(text_source):
            text += f'\n\n--\nUnsubscribe: {unsub}' + (f'\n{address}' if address else '')

        if tracking and campaign['track_clicks']:
            html = self.track_links(html, mid, campaign['id'], skip=(unsub,))
        if tracking and campaign['track_opens']:
            html = self.add_pixel(html, mid)
        return subject, html, text

    def campaign_message(self, campaign, contact, mid, settings, to_email=None, tracking=True,
                         content=None, ai_line=None, parent_id=None):
        subject, html, text = self.campaign_bodies(campaign, contact, mid, settings, tracking,
                                                   content=content, ai_line=ai_line)
        from_email = campaign['from_email'] or settings.get('from_email', '')
        from_name = campaign['from_name'] or settings.get('from_name', '')
        msg = build_message(
            mid=mid, from_email=from_email, from_name=from_name,
            to=[to_email or contact['email']], subject=subject, html=html, text=text,
            reply_to=campaign['reply_to'] or settings.get('reply_to', ''),
            host=self.cfg.HOSTNAME)
        unsub = self.unsubscribe_url(mid)
        msg['List-Unsubscribe'] = f'<{unsub}>'
        msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
        msg['Precedence'] = 'bulk'
        sender_id = re.sub(r'[^a-z0-9]+', '', (from_email.split('@')[-1] or 'sender').lower())
        msg['Feedback-ID'] = f'{campaign["id"]}:campaign:{sender_id}'
        if parent_id:
            # A follow-up in the same conversation as the earlier email.
            ref = message_id_for(parent_id, self.cfg.HOSTNAME, from_email)
            msg['In-Reply-To'] = ref
            msg['References'] = ref
        return msg

    # -- single / API mail -------------------------------------------------
    def single_message(self, row, settings):
        """Builds a message for kind = api | test | agent from its stored payload."""
        payload = loads(row['payload'])
        mid = row['id']
        html = payload.get('html') or ''
        text = payload.get('text')
        if html and text is None:
            text = html_to_text(html)
        if html and '<html' not in html.lower():
            html = f'<!doctype html><html><head><meta charset="utf-8"></head><body>{html}</body></html>'
        if html and payload.get('track', True):
            if settings.get('track_clicks') == '1':
                html = self.track_links(html, mid, 0)
            if settings.get('track_opens') == '1':
                html = self.add_pixel(html, mid)
        msg = build_message(
            mid=mid, from_email=payload.get('from_email', ''), from_name=payload.get('from_name', ''),
            to=payload.get('to') or [row['to_email']], cc=payload.get('cc'), subject=row['subject'],
            html=html, text=text or '', reply_to=', '.join(payload.get('reply_to') or []),
            host=self.cfg.HOSTNAME, extra_headers=payload.get('headers'))
        return msg


def message_id_for(mid, host, from_email):
    """The Message-ID header build_message gives a message."""
    return f'<{mid}@{host or from_email.split("@")[-1] or "localhost"}>'


class MailPolicy(email.policy.EmailPolicy):
    """Standard policy, except that machine-read headers are written on one
    line. The library otherwise turns a long URL into an encoded word, and a
    mail client can no longer read the unsubscribe link from it."""
    UNFOLDED = {'list-unsubscribe', 'list-unsubscribe-post', 'feedback-id', 'x-message-ref', 'message-id',
                'in-reply-to'}

    def fold(self, name, value):
        if name.lower() in self.UNFOLDED:
            return f'{name}: {value}{self.linesep}'
        return super().fold(name, value)

    def fold_binary(self, name, value):
        if name.lower() in self.UNFOLDED:
            return f'{name}: {value}{self.linesep}'.encode('ascii', 'replace')
        return super().fold_binary(name, value)


POLICY = MailPolicy()

RESERVED_HEADERS = {'from', 'to', 'cc', 'bcc', 'subject', 'date', 'message-id', 'content-type',
                    'content-transfer-encoding', 'mime-version', 'return-path', 'received',
                    'dkim-signature', 'x-message-ref', 'sender'}


def build_message(*, mid, from_email, from_name='', to, subject, html='', text='', reply_to='',
                  cc=None, host='', extra_headers=None):
    msg = EmailMessage(policy=POLICY)
    msg['From'] = from_header(from_name, from_email)
    msg['To'] = ', '.join(to)
    if cc:
        msg['Cc'] = ', '.join(cc)
    msg['Subject'] = ' '.join((subject or '').split())
    msg['Date'] = formatdate(localtime=False, usegmt=True)
    msg['Message-ID'] = message_id_for(mid, host, from_email)
    if reply_to:
        msg['Reply-To'] = reply_to
    msg['X-Message-Ref'] = mid
    for name, value in (extra_headers or {}).items():
        if name.lower() in RESERVED_HEADERS or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', name):
            continue
        del msg[name]
        msg[name] = ' '.join(str(value).split())[:900]
    msg.set_content(text or html_to_text(html) or ' ', cte='quoted-printable')
    if html:
        msg.add_alternative(html, subtype='html', cte='quoted-printable')
    return msg


def _insert_before_body_end(html, fragment):
    index = html.lower().rfind('</body>')
    if index == -1:
        return html + fragment
    return html[:index] + fragment + html[index:]


def _insert_after_body_start(html, fragment):
    match = re.search(r'(?i)<body\b[^>]*>', html)
    if not match:
        return fragment + html
    return html[:match.end()] + fragment + html[match.end():]
