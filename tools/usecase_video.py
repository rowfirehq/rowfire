#!/usr/bin/env python3
"""Render a ~20s vertical (1080x1920) YouTube Short for one Rowfire use case.

Usage:
    python3 tools/usecase_video.py <path/to/use-case.md> <out.mp4> [--hook "Opening line"]

--hook replaces the page title on the first slide with the post's opening
line (a question or a scenario), so the Short opens the same way the post
does. Keep it under ~70 characters.

The input is a use-case page from rowfirehq/rowfire-site
(src/content/use-cases/<slug>.md). Everything shown comes from its
frontmatter, so the video never says more than the page does.

Needs: Python 3 with Pillow and PyYAML, and ffmpeg on PATH.
"""
import os
import re
import subprocess
import sys
import tempfile

import yaml
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
M = 88  # side margin
HERE = os.path.dirname(os.path.abspath(__file__))

# Tokens from the product UI (ui/src/styles.css), light theme.
PAGE = "#f9f9f7"
SURFACE = "#fcfcfb"
SURFACE_2 = "#f1f0ec"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
MUTED = "#898781"
BORDER = "#e3e2de"
BRAND = "#6e56cf"
BRAND_WASH = "#ece8f9"
GOOD = "#0a8a0a"
CODE_BG = "#15141a"
CODE_TEXT = "#e9e7f5"
CODE_KW = "#b7a6ff"
CODE_DIM = "#8b88a0"

DATABASES = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MariaDB"}
DESTINATIONS = {
    "slack": ("Slack", "a message in a channel"),
    "zendesk": ("Zendesk", "a ticket for the team"),
    "braze": ("Braze", "an event that starts a campaign"),
    "webhook": ("Any REST API", "a call to your own service"),
}
SQL_KEYWORDS = {
    "select", "from", "where", "join", "left", "inner", "on", "and", "or", "not",
    "as", "is", "null", "group", "by", "order", "having", "count", "sum", "max",
    "min", "interval", "case", "when", "then", "else", "end", "with", "in",
    "exists", "distinct", "limit", "true", "false", "coalesce", "now", "date_sub",
    "over", "partition", "row_number", "between", "like", "lag", "desc", "asc",
}


def font(name, size):
    candidates = {
        "bold": ["/usr/share/fonts/opentype/inter/Inter-Bold.otf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        "semibold": ["/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
        "regular": ["/usr/share/fonts/opentype/inter/Inter-Regular.otf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "medium": ["/usr/share/fonts/opentype/inter/Inter-Medium.otf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
        "mono": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"],
    }[name]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def wrap(draw, text, fnt, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def text_block(draw, xy, text, fnt, fill, width, gap=1.25):
    x, y = xy
    size = fnt.size
    for line in wrap(draw, text, fnt, width):
        draw.text((x, y), line, font=fnt, fill=fill)
        y += int(size * gap)
    return y


def new_slide():
    img = Image.new("RGB", (W, H), PAGE)
    return img, ImageDraw.Draw(img)


def header(img, draw, step=None, label=""):
    logo = Image.open(os.path.join(HERE, "assets", "logo.png")).convert("RGBA").resize((72, 72))
    img.paste(logo, (M, 120), logo)
    draw.text((M + 92, 132), "Rowfire", font=font("semibold", 42), fill=TEXT)
    if step:
        y = 380
        pill = f"{step}  ·  {label}"
        f = font("semibold", 34)
        tw = draw.textlength(pill, font=f)
        draw.rounded_rectangle((M, y, M + tw + 56, y + 70), radius=35, fill=BRAND_WASH)
        draw.text((M + 28, y + 15), pill, font=f, fill=BRAND)


FOOTER_Y = H - 470


def footer(draw, slug, y=FOOTER_Y):
    url = f"rowfire.com/use-cases/{slug}/"
    size = 30
    while size > 20 and draw.textlength(url, font=font("medium", size)) > W - 2 * M:
        size -= 1
    if draw.textlength(url, font=font("medium", size)) > W - 2 * M:
        url = "rowfire.com/use-cases/"
    draw.text((M, y), url, font=font("medium", size), fill=MUTED)


def wrap_sql(lines, cpl):
    out = []
    for line in lines:
        indent = len(line) - len(line.lstrip())
        while len(line) > cpl:
            cut = line.rfind(" ", indent + 1, cpl)
            if cut <= indent:
                cut = cpl
            out.append(line[:cut].rstrip())
            line = " " * (indent + 4) + line[cut:].lstrip()
        out.append(line)
    return out


def slide_hook(fm, slug, hook=None):
    img, d = new_slide()
    header(img, d)
    y = 600
    team = f"FOR {fm['team'].upper()} TEAMS"
    d.text((M, y), team, font=font("semibold", 36), fill=BRAND)
    y += 90
    headline = hook or fm["title"]
    size = 92 if len(headline) <= 60 else 76
    y = text_block(d, (M, y), headline, font("bold", size), TEXT, W - 2 * M, 1.12)
    if not hook:
        y += 50
        text_block(d, (M, y), fm["description"], font("regular", 44), TEXT_2, W - 2 * M, 1.4)
        footer(d, slug)
    return img


def highlight_sql(d, x, y, line, fnt):
    for token in re.split(r"(\s+|[(),.;=<>*+-]|'[^']*')", line):
        if not token:
            continue
        if token.lower() in SQL_KEYWORDS:
            color = CODE_KW
        elif token.startswith("'") or re.fullmatch(r"\d+", token):
            color = "#9fe0b0"
        else:
            color = CODE_TEXT
        d.text((x, y), token, font=fnt, fill=color)
        x += d.textlength(token, font=fnt)


def slide_sql(fm, slug):
    img, d = new_slide()
    header(img, d, "1", "Engineering, once")
    y = 500
    y = text_block(d, (M, y), "Describe the event as one SQL query.", font("bold", 64), TEXT, W - 2 * M, 1.15)
    y += 50
    raw_lines = fm["sql"].rstrip().splitlines()
    pad = 48
    inner = W - 2 * M - 2 * pad
    top_bar = 76
    max_box = FOOTER_Y - 110 - y  # leave room for the caption above the footer
    for size in range(34, 21, -1):
        cpl = int(inner // d.textlength("M", font=font("mono", size)))
        lines = wrap_sql(raw_lines, cpl)
        line_h = int(size * 1.55)
        box_h = top_bar + pad + line_h * len(lines) + pad - int(size * 0.55)
        if box_h <= max_box:
            break
    mono = font("mono", size)
    d.rounded_rectangle((M, y, W - M, y + box_h), radius=28, fill=CODE_BG)
    db = DATABASES.get(fm["database"], fm["database"])
    d.text((M + pad, y + 24), f"{db}  ·  read-only", font=font("medium", 28), fill=CODE_DIM)
    cy = y + top_bar + pad - 10
    for line in lines:
        highlight_sql(d, M + pad, cy, line, mono)
        cy += line_h
    y += box_h + 50
    y = text_block(d, (M, y), "Rowfire polls it and never writes to your database.",
                   font("regular", 38), TEXT_2, W - 2 * M, 1.4)
    footer(d, slug, max(FOOTER_Y, y + 20))
    return img


def slide_rule(fm, slug):
    img, d = new_slide()
    header(img, d, "2", f"{fm['team']}, on their own")
    y = 500
    y = text_block(d, (M, y), "Subscribe to it, say how often, pick the action.",
                   font("bold", 64), TEXT, W - 2 * M, 1.15)
    y += 70
    # Cadence card
    cad_font = font("semibold", 46)
    lines = wrap(d, fm["cadence"], cad_font, W - 2 * M - 88)
    card_h = 130 + 58 * len(lines)
    d.rounded_rectangle((M, y, W - M, y + card_h), radius=28, fill=SURFACE, outline=BORDER, width=2)
    d.text((M + 44, y + 36), "HOW OFTEN", font=font("semibold", 28), fill=MUTED)
    for i, line in enumerate(lines):
        d.text((M + 44, y + 90 + 58 * i), line, font=cad_font, fill=TEXT)
    y += card_h + 32
    # Destinations
    for key in fm["destinations"]:
        name, what = DESTINATIONS.get(key, (key, ""))
        d.rounded_rectangle((M, y, W - M, y + 150), radius=28, fill=SURFACE, outline=BORDER, width=2)
        d.ellipse((M + 44, y + 59, M + 76, y + 91), fill=BRAND)
        d.text((M + 104, y + 30), name, font=font("semibold", 46), fill=TEXT)
        d.text((M + 104, y + 88), what, font=font("regular", 34), fill=TEXT_2)
        y += 150 + 24
    footer(d, slug)
    return img


def slide_safe(fm, slug):
    img, d = new_slide()
    header(img, d, "3", "See it before it sends")
    y = 500
    y = text_block(d, (M, y), "Check it against history, then go live.",
                   font("bold", 64), TEXT, W - 2 * M, 1.15)
    y += 70
    steps = [
        ("Backtest", "Replays the rule over the last N days and shows who it would have reached."),
        ("Shadow", "Renders every real request without sending it."),
        ("Live", "Promote it when the numbers look right."),
    ]
    for i, (name, what) in enumerate(steps):
        d.ellipse((M, y, M + 72, y + 72), fill=GOOD if name == "Live" else BRAND)
        n = str(i + 1)
        f = font("bold", 36)
        d.text((M + 36 - d.textlength(n, font=f) / 2, y + 14), n, font=f, fill="#ffffff")
        d.text((M + 104, y + 6), name, font=font("semibold", 50), fill=TEXT)
        y2 = text_block(d, (M + 104, y + 76), what, font("regular", 36), TEXT_2, W - 2 * M - 104, 1.35)
        if i < len(steps) - 1:
            d.line((M + 36, y + 84, M + 36, y2 + 26), fill=BORDER, width=4)
        y = y2 + 40
    footer(d, slug)
    return img


def slide_cta(fm, slug):
    img, d = new_slide()
    logo = Image.open(os.path.join(HERE, "assets", "logo.png")).convert("RGBA").resize((160, 160))
    img.paste(logo, ((W - 160) // 2, 600), logo)
    f = font("bold", 84)
    t = "Rowfire"
    d.text(((W - d.textlength(t, font=f)) / 2, 800), t, font=f, fill=TEXT)
    f2 = font("regular", 44)
    y = 940
    for line in wrap(d, "The events your app never emitted.", f2, W - 2 * M):
        d.text(((W - d.textlength(line, font=f2)) / 2, y), line, font=f2, fill=TEXT_2)
        y += 60
    y += 80
    btn = "Try the demo: demo.rowfire.com"
    f3 = font("semibold", 42)
    bw = d.textlength(btn, font=f3) + 100
    d.rounded_rectangle(((W - bw) / 2, y, (W + bw) / 2, y + 110), radius=55, fill=BRAND)
    d.text(((W - d.textlength(btn, font=f3)) / 2, y + 30), btn, font=f3, fill="#ffffff")
    y += 180
    f4 = font("medium", 34)
    t4 = "Open source · PostgreSQL, MySQL & MariaDB"
    d.text(((W - d.textlength(t4, font=f4)) / 2, y), t4, font=f4, fill=MUTED)
    return img


def main(src, out, hook=None):
    raw = open(src, encoding="utf-8").read()
    fm = yaml.safe_load(raw.split("---", 2)[1])
    slug = os.path.splitext(os.path.basename(src))[0]
    slides = [  # (image, seconds on screen)
        (slide_hook(fm, slug, hook), 5.0),
        (slide_sql(fm, slug), 6.0),
        (slide_rule(fm, slug), 5.0),
        (slide_safe(fm, slug), 4.5),
        (slide_cta(fm, slug), 3.5),
    ]
    fade = 0.4
    if os.environ.get("FRAMES_ONLY"):
        for i, (img, _) in enumerate(slides):
            img.save(f"{os.path.splitext(out)[0]}-{i}.png")
        return
    with tempfile.TemporaryDirectory() as tmp:
        inputs, filters = [], []
        for i, (img, secs) in enumerate(slides):
            p = os.path.join(tmp, f"s{i}.png")
            img.save(p)
            inputs += ["-loop", "1", "-t", str(secs), "-i", p]
            filters.append(f"[{i}:v]fps=30,format=yuv420p,setsar=1[v{i}]")
        # Chain crossfades.
        prev, offset = "v0", 0.0
        for i in range(1, len(slides)):
            offset += slides[i - 1][1] - fade
            filters.append(f"[{prev}][v{i}]xfade=transition=fade:duration={fade}:offset={offset:.2f}[x{i}]")
            prev = f"x{i}"
        total = sum(s for _, s in slides) - fade * (len(slides) - 1)
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
               "-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=r=48000:cl=stereo",
               "-filter_complex", ";".join(filters),
               "-map", f"[{prev}]", "-map", f"{len(slides)}:a",
               "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out]
        subprocess.run(cmd, check=True)
        if os.environ.get("KEEP_FRAMES"):
            for i, (img, _) in enumerate(slides):
                img.save(f"{os.path.splitext(out)[0]}-{i}.png")
    print(f"{out}  ({total:.1f}s)")


if __name__ == "__main__":
    args = sys.argv[1:]
    hook = None
    if "--hook" in args:
        i = args.index("--hook")
        hook = args[i + 1].strip()
        del args[i:i + 2]
    if len(args) != 2:
        sys.exit(__doc__)
    main(args[0], args[1], hook)
