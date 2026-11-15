"""AI text for people: safe HTML for the chat, and spotting phrases that make
writing read as machine-made.

The chat never trusts model output as HTML: everything is escaped first, then a
small set of Markdown (bold, italics, code, lists, links, simple tables) is
turned back into tags.
"""
import html
import re

from markupsafe import Markup

_CODE_RE = re.compile(r'`([^`\n]{1,400})`')
_LINK_RE = re.compile(r'\[([^\]\n]{1,300})\]\((https?://[^\s)]{1,800})\)')
_URL_RE = re.compile(r'(?<![\w/"=])(https?://[^\s<>()"\']+[^\s<>()"\'.,;:!?])')
_BOLD_RE = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*')
_ITALIC_RE = re.compile(r'(?<![*\w])[*_](?=[^\s*_])([^*_\n]+?)(?<=[^\s*_])[*_](?![*\w])')
_BULLET_RE = re.compile(r'^\s{0,3}(?:[-*+•])\s+(.*)$')
_NUMBER_RE = re.compile(r'^\s{0,3}(\d{1,3})[.)]\s+(.*)$')
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$')
_RULE_RE = re.compile(r'^\s{0,3}([-*_])(\s*\1){2,}\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def _inline(text):
    """One line of Markdown -> safe HTML."""
    codes = []

    def keep_code(match):
        codes.append(f'<code>{match.group(1)}</code>')
        return f'\x00{len(codes) - 1}\x00'

    out = html.escape(text, quote=True)
    out = _CODE_RE.sub(keep_code, out)
    links = []

    def keep_link(match):
        links.append(f'<a href="{match.group(2)}" target="_blank" rel="noopener noreferrer">{match.group(1)}</a>')
        return f'\x01{len(links) - 1}\x01'

    def keep_url(match):
        url = match.group(1)
        links.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{_short_url(url)}</a>')
        return f'\x01{len(links) - 1}\x01'

    out = _LINK_RE.sub(keep_link, out)
    out = _URL_RE.sub(keep_url, out)
    out = _BOLD_RE.sub(r'<strong>\1</strong>', out)
    out = _ITALIC_RE.sub(r'<em>\1</em>', out)
    out = re.sub('\x01(\\d+)\x01', lambda m: links[int(m.group(1))], out)
    return re.sub('\x00(\\d+)\x00', lambda m: codes[int(m.group(1))], out)


def _short_url(url):
    """https://www.example.com/some/long/path -> example.com/some/long/path, shortened."""
    shown = re.sub(r'^https?://(www\.)?', '', url).rstrip('/')
    return shown if len(shown) <= 60 else shown[:57] + '…'


def _cells(line):
    return [c.strip() for c in line.strip().strip('|').split('|')]


def markdown(text):
    """Model text -> safe HTML (Markup). Unknown syntax stays as plain text."""
    lines = str(text or '').replace('\r\n', '\n').split('\n')
    out, para, items, kind, i = [], [], [], None, 0

    def flush_para():
        if para:
            out.append('<p>' + '<br>'.join(_inline(p) for p in para) + '</p>')
            para.clear()

    def flush_list():
        nonlocal kind
        if items:
            tag = 'ol' if kind == 'ol' else 'ul'
            out.append(f'<{tag}>' + ''.join(f'<li>{_inline(item)}</li>' for item in items) + f'</{tag}>')
            items.clear()
        kind = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith('```'):
            flush_para()
            flush_list()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                block.append(lines[i])
                i += 1
            out.append('<pre><code>' + html.escape('\n'.join(block)) + '</code></pre>')
            i += 1
            continue
        if stripped.startswith('|') and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            flush_list()
            head = _cells(stripped)
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(_cells(lines[i]))
                i += 1
            table = '<div class="md-table"><table><thead><tr>' + ''.join(f'<th>{_inline(c)}</th>' for c in head)
            table += '</tr></thead><tbody>'
            for row in rows:
                table += '<tr>' + ''.join(f'<td>{_inline(c)}</td>' for c in row) + '</tr>'
            out.append(table + '</tbody></table></div>')
            continue
        if not stripped:
            flush_para()
            flush_list()
        elif _RULE_RE.match(line):
            flush_para()
            flush_list()
        elif _HEADING_RE.match(line):
            flush_para()
            flush_list()
            out.append(f'<p class="md-h">{_inline(_HEADING_RE.match(line).group(1))}</p>')
        elif _BULLET_RE.match(line) or _NUMBER_RE.match(line):
            flush_para()
            bullet = _BULLET_RE.match(line)
            new_kind = 'ul' if bullet else 'ol'
            if kind and kind != new_kind:
                flush_list()
            kind = new_kind
            items.append(bullet.group(1) if bullet else _NUMBER_RE.match(line).group(2))
        elif items and line.startswith(('  ', '\t')):
            items[-1] += ' ' + stripped           # a wrapped list item
        else:
            flush_list()
            para.append(stripped)
        i += 1
    flush_para()
    flush_list()
    return Markup(''.join(out))


# -- writing that sounds like a person ------------------------------------------------
# Phrases that make outreach read as written by a machine (or a mass mailer).
AI_PHRASES = (
    r'hope (this|the|my) (email|message|note) finds you', r'hope you(\'re| are) (doing )?well', r'i came across',
    r'i stumbled (up)?on', r'i noticed (that )?', r'i was (really |very )?impressed', r'caught my (eye|attention)',
    r'i wanted to reach out', r'i(\'m| am) reaching out', r'reaching out (to|because)', r'i(\'d| would) love to',
    r'just wanted to', r'touch base', r'circle back', r'\bdelve', r'\belevate', r'\bleverag', r'\bunlock',
    r'\bseamless', r'game[- ]?changer', r'cutting[- ]edge', r'next level', r'in today\'s', r'testament to',
    r'rest assured', r'don\'t hesitate', r'feel free to', r'look no further', r'\bsynerg', r'\brevolutioni[sz]',
    r'\bempower', r'resonate[sd]? with', r'(truly|incredibly) (impressive|inspiring)', r'impressive (work|job)',
    r'amazing (work|job)', r'love what you(\'re| are) doing', r'\bthrilled\b', r'excited to (share|offer)',
    r'\bdive (deep|into)', r'navigate the', r'\blandscape\b', r'in the realm of', r'\bjourney\b',
    r'\b(our|special|exclusive|limited|this|an) offer\b', r'we(\'re| are) offering', r'i(\'d| would) like to offer',
    r'we have an? (offer|deal|special|exclusive|great opportunity)',
)
_AI_RE = re.compile('|'.join(f'(?:{p})' for p in AI_PHRASES), re.IGNORECASE)


def ai_tells(text):
    """Machine-sounding phrases (and long dashes) in a piece of writing, as found."""
    text = str(text or '')
    found = []
    for match in _AI_RE.finditer(text):
        phrase = match.group(0).strip().lower()
        if phrase not in found:
            found.append(phrase)
    if '—' in text or ' – ' in text:
        found.append('long dash (—)')
    if text.count('!') > 1:
        found.append('several exclamation marks')
    return found


def plain_punctuation(text):
    """Long dashes become what a person would type."""
    text = str(text or '')
    text = re.sub(r'(\d)\s*[–—]\s*(\d)', r'\1-\2', text)          # 3–4 days -> 3-4 days
    text = re.sub(r'\s*—\s*', ', ', text)                          # aside — like this -> aside, like this
    text = re.sub(r'\s+–\s+', ', ', text)
    text = re.sub(r',\s*,', ',', text)
    return re.sub(r',\s*([.!?])', r'\1', text)


STYLE_RULES = """How the writing must sound
- Like a busy person typing a short note to someone they respect. Plain everyday words, short sentences, \
contractions are fine.
- Get to the point in the first sentence. No warm-up about how you found them or why you are writing.
- One specific, true detail about them beats any compliment. No flattery ("impressive", "amazing", "love what \
you're doing").
- No long dashes (— or –): use a comma, a full stop or brackets. No exclamation marks, emoji, buzzwords or \
rhetorical questions.
- Never use these phrases or close variants: "I hope this finds you well", "I came across", "I noticed", "caught \
my eye", "I wanted to reach out", "I'm reaching out", "I'd love to", "just wanted to", "touch base", "circle back", \
"leverage", "elevate", "seamless", "game-changer", "cutting-edge", "delve", "unlock", "take it to the next level", \
"don't hesitate", "feel free to".
- Never sell with "we have an offer", "our offer", "special offer" or "we are offering". Talk about them: what is \
happening for them, what it costs them, and what would change."""

EMAIL_STRUCTURE = """How a first email is built (in this order, short paragraphs)
1. Their situation: one true, specific thing about them (from their website, the notes or their details).
2. The problem: what it costs them today and why it matters now. This is why they need it.
3. How it helps them: the change they would see, in their terms. Do not describe it as an offer.
4. Proof: up to three of the sender's case studies, one short line each ("A tutoring centre in Leeds had ...; \
after ..., ..."). Use only the case studies given, keep their facts and numbers exactly, and leave this part out \
when none are given. Never invent a client, a number or a result.
5. One small, easy next step (a reply, a 10-minute call, a link to look at).
Under 180 words in total. No header, signature or footer: those are added automatically."""


__all__ = ['AI_PHRASES', 'STYLE_RULES', 'ai_tells', 'markdown', 'plain_punctuation']
