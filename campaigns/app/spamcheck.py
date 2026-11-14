"""Pre-send content check.

Two sources: the server's own rspamd (the same engine many receivers run) when
it is reachable, and a built-in checklist of the content problems that most
often send legitimate mail to the junk folder. The goal is to fix the message,
not to disguise it.
"""
import re

import requests

from .render import html_to_text

SHORTENERS = ('bit.ly', 'tinyurl.com', 'goo.gl', 't.co', 'ow.ly', 'is.gd', 'buff.ly', 'cutt.ly', 'rebrand.ly')
TRIGGER_PHRASES = (
    'act now', 'limited time', 'risk free', 'risk-free', '100% free', 'free money', 'winner',
    'you have been selected', 'click here', 'buy now', 'order now', 'guaranteed', 'no obligation',
    'earn money', 'make money', 'cash bonus', 'double your', 'cheap', 'lowest price', 'urgent',
    'congratulations', 'this is not spam', 'viagra', 'casino', 'lottery', 'miracle', 'weight loss',
)


def lint(subject, html, text, from_email='', has_unsubscribe=True):
    """Returns a list of {'level': 'bad'|'warn'|'ok', 'title', 'advice'}."""
    findings = []

    def add(level, title, advice=''):
        findings.append({'level': level, 'title': title, 'advice': advice})

    subject = subject or ''
    plain = text or html_to_text(html or '')
    words = len(plain.split())

    if not subject.strip():
        add('bad', 'Subject is empty', 'Write a short, specific subject line.')
    else:
        if len(subject) > 78:
            add('warn', f'Subject is long ({len(subject)} characters)',
                'Most inboxes cut subjects off around 60 characters.')
        letters = [c for c in subject if c.isalpha()]
        if len(letters) >= 8 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            add('bad', 'Subject is mostly capital letters', 'Use normal sentence case.')
        if subject.count('!') > 1 or re.search(r'[!?]{2,}', subject):
            add('warn', 'Subject has repeated punctuation', 'Use at most one exclamation mark.')
        if re.search(r'(?i)^\s*(re|fwd?):', subject):
            add('bad', 'Subject pretends to be a reply or forward',
                'A "Re:" on a first email is treated as deceptive by filters and by law in many places.')
        if re.search(r'[$€£]\s?\d|\d\s?%\s?off', subject, re.IGNORECASE):
            add('warn', 'Subject leads with a price or discount', 'Fine occasionally; avoid making it the whole subject.')

    lowered = f'{subject}\n{plain}'.lower()
    hits = sorted({p for p in TRIGGER_PHRASES if re.search(rf'(?<![a-z]){re.escape(p)}(?![a-z])', lowered)})
    if len(hits) >= 3:
        add('bad', 'Several phrases that filters associate with junk', 'Reword: ' + ', '.join(hits[:8]))
    elif hits:
        add('warn', 'Phrases that filters associate with junk', 'Consider rewording: ' + ', '.join(hits))

    if html:
        images = len(re.findall(r'(?i)<img\b', html))
        links = re.findall(r'(?is)<a\b[^>]*?href\s*=\s*["\'](.*?)["\'][^>]*>(.*?)</a>', html)
        if images and words < 20:
            add('bad', 'Message is almost only images',
                'Add real text. Image-only mail is a strong junk signal and is unreadable with images off.')
        elif images > 0 and words / max(images, 1) < 40 and images > 3:
            add('warn', 'Many images for the amount of text', 'Aim for at least a few sentences per image.')
        if re.search(r'(?i)<img\b(?![^>]*\balt\s*=)', html):
            add('warn', 'Images without alt text', 'Add alt="..." so the message makes sense with images off.')
        if len(links) > 15:
            add('warn', f'{len(links)} links in one message', 'Keep to the few links that matter.')
        short = sorted({s for url, _ in links for s in SHORTENERS if re.search(rf'(?i)://(www\.)?{re.escape(s)}/', url)})
        if short:
            add('bad', 'Link shorteners used: ' + ', '.join(short),
                'Link to the real address. Shorteners hide the destination and are heavily abused.')
        for url, label in links:
            label_text = re.sub(r'(?s)<[^>]+>', '', label).strip().lower()
            shown = re.match(r'(?:https?://)?(?:www\.)?([a-z0-9.-]+\.[a-z]{2,})', label_text)
            actual = re.match(r'(?i)https?://(?:www\.)?([a-z0-9.-]+)', url.strip())
            if shown and actual and not actual.group(1).lower().endswith(shown.group(1)) \
                    and '{{' not in url:
                add('bad', 'A link shows one address but opens another',
                    f'"{label_text[:60]}" opens {actual.group(1)}. Make the visible text match or use words instead.')
                break
        if re.search(r'(?i)<(script|iframe|form|object|embed)\b', html):
            add('bad', 'Message contains script, form or embedded content',
                'Mail clients strip these and filters penalise them. Link to a web page instead.')
        if len(html) > 100_000:
            add('warn', f'HTML is large ({len(html) // 1024} KB)', 'Gmail clips messages over about 102 KB.')
        if re.search(r'(?i)font-size\s*:\s*0|color\s*:\s*#?fff(fff)?\s*;[^"]*background(-color)?\s*:\s*#?fff', html):
            add('bad', 'Hidden text detected', 'Remove invisible text; filters treat it as an evasion attempt.')
    if words < 5:
        add('warn', 'Very little text', 'Add a sentence or two explaining why the reader is getting this.')
    if not has_unsubscribe:
        add('bad', 'No unsubscribe link', 'An unsubscribe link is added automatically; do not remove it.')
    if from_email and re.match(r'(?i)(no-?reply|donotreply)@', from_email):
        add('warn', 'Sending from a no-reply address',
            'Replies are a positive signal to inbox providers. Use an address someone reads.')
    free = ('gmail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'aol.com', 'icloud.com')
    if from_email and from_email.rsplit('@', 1)[-1].lower() in free:
        add('bad', 'From address is on a free mail provider',
            'Their DMARC policy makes mail sent from your own server fail. Send from your own domain.')
    if not findings:
        add('ok', 'No content problems found')
    return findings


def rspamd_check(url, raw_message, from_email='', recipient='', timeout=10):
    """Scores a message with rspamd. Returns a dict or None when unreachable."""
    headers = {'Content-Type': 'message/rfc822'}
    if from_email:
        headers['From'] = from_email
    if recipient:
        headers['Rcpt'] = recipient
    try:
        reply = requests.post(url.rstrip('/') + '/checkv2', data=raw_message, headers=headers, timeout=timeout)
        if reply.status_code != 200:
            return None
        data = reply.json()
    except (requests.RequestException, ValueError):
        return None
    symbols = []
    for name, info in (data.get('symbols') or {}).items():
        score = info.get('score') or 0
        if abs(score) >= 0.1:
            symbols.append({'name': name, 'score': round(score, 2), 'description': info.get('description') or ''})
    symbols.sort(key=lambda s: -s['score'])
    return {'score': round(data.get('score', 0), 2), 'required': data.get('required_score'),
            'action': data.get('action', ''), 'symbols': symbols[:25]}


def verdict(findings, rspamd):
    """Overall 'good' | 'review' | 'fix' for display and for the agent."""
    if any(f['level'] == 'bad' for f in findings):
        return 'fix'
    if rspamd and rspamd.get('required') and rspamd['score'] >= rspamd['required'] * 0.5:
        return 'fix' if rspamd['score'] >= rspamd['required'] else 'review'
    if any(f['level'] == 'warn' for f in findings):
        return 'review'
    return 'good'
