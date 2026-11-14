"""Generates the logo and favicon set for your brand.

    python branding/make_icons.py --name "Your Brand" --color "#5b3be8"

Writes into branding/static/, which the web proxy serves at the site root
(/logo.svg, /favicon.ico, ...). Re-run it whenever you change the colour, or
replace the files with your own artwork: keep the file names.

The mark is a paper plane leaving a dot ("home") on a tile that shades from
the brand colour into --accent, like the sheen on a homing pigeon's neck.

Needs Pillow:  pip install pillow
"""
import argparse
import json
import os
import re

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'static')

# Paper plane on a 64-unit grid: tip, left wing, centre fold, tail.
PLANE = [(51, 13), (12, 30), (28, 35), (35, 52)]
FOLD = [(51, 13), (28, 35), (35, 52)]
HOME = (15, 50)
DOTS = [(17.6, 43.2), (20.8, 39.6), (24.6, 37.6)]  # the trail from the home dot to the plane

MARK = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="{name}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{color}"/><stop offset="1" stop-color="{accent}"/></linearGradient></defs>
  <rect width="64" height="64" rx="16" fill="{fill}"/>
  <g fill="#fff" opacity="0.9"><circle cx="17.6" cy="43.2" r="1.6"/><circle cx="20.8" cy="39.6" r="1.6"/><circle cx="24.6" cy="37.6" r="1.6"/></g>
  <circle cx="15" cy="50" r="4" fill="#fff"/>
  <path d="M51 13 12 30l16 5 7 17z" fill="#fff"/>
  <path d="M51 13 28 35l7 17z" fill="#fff" opacity="0.7"/>
</svg>
'''

WORDMARK = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 64" role="img" aria-label="{name}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{color}"/><stop offset="1" stop-color="{accent}"/></linearGradient></defs>
  <rect width="64" height="64" rx="16" fill="url(#g)"/>
  <g fill="#fff" opacity="0.9"><circle cx="17.6" cy="43.2" r="1.6"/><circle cx="20.8" cy="39.6" r="1.6"/><circle cx="24.6" cy="37.6" r="1.6"/></g>
  <circle cx="15" cy="50" r="4" fill="#fff"/>
  <path d="M51 13 12 30l16 5 7 17z" fill="#fff"/>
  <path d="M51 13 28 35l7 17z" fill="#fff" opacity="0.7"/>
  <text x="80" y="45" font-family="Archivo, 'Arial Narrow', Arial, sans-serif" font-weight="800" font-size="38"
        letter-spacing="-1" fill="{ink}">{name}</text>
</svg>
'''


def _rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))


def draw_icon(size, color, accent):
    """The same mark as the SVG, drawn 4x and scaled down for smooth edges."""
    scale = 4
    big = size * scale
    unit = big / 64.0

    # Diagonal gradient, built small and scaled up (it is smooth anyway).
    ramp = Image.new('L', (64, 64))
    ramp.putdata([min(255, int((x + y) * 255 / 126)) for y in range(64) for x in range(64)])
    ramp = ramp.resize((big, big), Image.BILINEAR)
    tile = Image.composite(Image.new('RGBA', (big, big), _rgb(accent)), Image.new('RGBA', (big, big), _rgb(color)), ramp)
    mask = Image.new('L', (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=int(16 * unit), fill=255)
    image = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    image.paste(tile, (0, 0), mask)

    draw = ImageDraw.Draw(image, 'RGBA')
    pts = lambda seq: [(x * unit, y * unit) for x, y in seq]  # noqa: E731
    if size >= 48:  # the dotted trail only reads at larger sizes
        for x, y in DOTS:
            r = 1.6 * unit
            draw.ellipse([x * unit - r, y * unit - r, x * unit + r, y * unit + r], fill=(255, 255, 255, 230))
    hx, hy = HOME
    r = 4 * unit
    draw.ellipse([hx * unit - r, hy * unit - r, hx * unit + r, hy * unit + r], fill='white')
    draw.polygon(pts(PLANE), fill='white')
    draw.polygon(pts(FOLD), fill=(255, 255, 255, 179))
    # The fold is drawn over white, so tint it toward the tile to show the crease.
    crease = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(crease).polygon(pts(FOLD), fill=_rgb(color) + (70,))
    image = Image.alpha_composite(image, crease)
    return image.resize((size, size), Image.LANCZOS)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--name', default='Homing')
    parser.add_argument('--color', default='#5b3be8', help='brand colour, the top-left of the tile')
    parser.add_argument('--accent', default='#17a673', help='second colour, the bottom-right of the tile')
    args = parser.parse_args()
    for value in (args.color, args.accent):
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            parser.error('colours must look like #5b3be8')
    os.makedirs(OUT, exist_ok=True)
    name = args.name.replace('"', '').replace('<', '').replace('&', '')

    def write(filename, text):
        with open(os.path.join(OUT, filename), 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)

    write('logo.svg', MARK.format(name=name, color=args.color, accent=args.accent, fill='url(#g)'))
    write('logo-wordmark.svg', WORDMARK.format(name=name, color=args.color, accent=args.accent, ink='#1a1830'))
    write('logo-wordmark-white.svg', WORDMARK.format(name=name, color=args.color, accent=args.accent, ink='#ffffff'))
    write('safari-pinned-tab.svg', MARK.format(name=name, color='#000000', accent='#000000', fill='#000000'))
    for filename, size in (('favicon-16x16.png', 16), ('favicon-32x32.png', 32), ('apple-touch-icon.png', 180),
                           ('android-chrome-192x192.png', 192), ('android-chrome-512x512.png', 512),
                           ('mstile-150x150.png', 150), ('logo.png', 256)):
        draw_icon(size, args.color, args.accent).save(os.path.join(OUT, filename))
    draw_icon(64, args.color, args.accent).save(os.path.join(OUT, 'favicon.ico'), sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])

    manifest = {
        'name': args.name, 'short_name': args.name,
        'icons': [{'src': '/android-chrome-192x192.png', 'sizes': '192x192', 'type': 'image/png'},
                  {'src': '/android-chrome-512x512.png', 'sizes': '512x512', 'type': 'image/png'}],
        'theme_color': args.color, 'background_color': '#ffffff', 'display': 'standalone'}
    write('site.webmanifest', json.dumps(manifest, indent=2))
    write('browserconfig.xml', '<?xml version="1.0" encoding="utf-8"?>\n<browserconfig><msapplication><tile>'
                               '<square150x150logo src="/mstile-150x150.png"/>'
                               f'<TileColor>{args.color}</TileColor></tile></msapplication></browserconfig>\n')
    write('robots.txt', 'User-agent: *\nDisallow: /\n')
    print(f'Wrote brand files for "{args.name}" ({args.color} to {args.accent}) to {OUT}')


if __name__ == '__main__':
    main()
