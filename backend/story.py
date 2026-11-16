"""The "story card" layout: black page, channel header, a big paragraph with coloured key phrases, the video in a
rounded box, and a short comment underneath. Everything except the video is drawn once into a PNG with a
transparent rounded hole that the video shows through."""
import re
from pathlib import Path

from . import config

W, H = 1080, 1920
BOX_X, BOX_W = 20, 1040
COLOURS = [(250, 240, 70), (80, 228, 240), (255, 59, 59), (92, 255, 80)]   # yellow, cyan, red, green
WHITE = (255, 255, 255)
GREY = (190, 190, 190)


def _font(names, size):
    from PIL import ImageFont
    for n in names:
        p = Path(n)
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


BOLD = [str(config.ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"), r"C:\Windows\Fonts\arialbd.ttf"]
SERIF = [r"C:\Windows\Fonts\georgia.ttf", r"C:\Windows\Fonts\times.ttf", *BOLD]


def _tokens(text: str):
    """Words with a highlight index (or None). Phrases in [square brackets] are highlighted; with no brackets,
    a few long words are picked automatically."""
    text = " ".join((text or "").split())
    out, n = [], 0
    if "[" in text:
        for m in re.finditer(r"\[([^\]]+)\]|([^\[\]]+)", text):
            if m.group(1):
                out += [(w, n) for w in m.group(1).split()]
                n += 1
            else:
                for w in m.group(2).split():
                    if out and m.group(2)[:1] not in " " and w == m.group(2).split()[0] and not w[:1].isalnum():
                        out[-1] = (out[-1][0] + w, out[-1][1])   # "phrase]." keeps its punctuation attached
                    else:
                        out.append((w, None))
        return out
    words = text.split()
    last = -9
    for i, w in enumerate(words):
        core = re.sub(r"\W", "", w)
        if len(core) >= 6 and i - last >= 5:
            out.append((w, n))
            n, last = n + 1, i
        else:
            out.append((w, None))
    return out


def _wrap(draw, tokens, font, width):
    lines, cur, cur_w = [], [], 0
    space = draw.textlength(" ", font=font)
    for w, c in tokens:
        tw = draw.textlength(w, font=font)
        if cur and cur_w + space + tw > width:
            lines.append(cur)
            cur, cur_w = [], 0
        cur_w += (space if cur else 0) + tw
        cur.append((w, c))
    if cur:
        lines.append(cur)
    return lines


def box_height(src_w: int, src_h: int) -> int:
    return int(min(780, max(600, BOX_W * src_h / max(1, src_w)))) // 2 * 2


def make_card(out: Path, story: str, comment: str, settings: dict, src_w: int, src_h: int) -> dict:
    """Draws the card and returns where the video box goes: {"x", "y", "w", "h"}."""
    from PIL import Image, ImageDraw
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    bh = box_height(src_w, src_h)
    name = (settings.get("channel_name") or "").strip()
    handle = (settings.get("channel_handle") or "").strip()
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    header_h = 190 if (name or handle) else 70

    toks = _tokens(story)
    cfont = _font(SERIF, 36)
    clines = _wrap(probe, [(w, None) for w in (comment or "").split()], cfont, 920)
    c_h = len(clines) * 48
    for size in range(68, 36, -2):
        pfont = _font(BOLD, size)
        plines = _wrap(probe, toks, pfont, 960) if toks else []
        lh = int(size * 1.32)
        p_h = len(plines) * lh
        total = header_h + p_h + (45 if p_h else 0) + bh + (35 + c_h if c_h else 0)
        if total <= H - 120:
            break
    top = header_h + max(0, (H - 120 - total) // 4)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    if name or handle:
        _header(img, d, name, handle, settings.get("channel_avatar") or "")
    y = top
    for line in plines:
        widths = [d.textlength(w, font=pfont) for w, _ in line]
        space = d.textlength(" ", font=pfont)
        x = (W - (sum(widths) + space * (len(line) - 1))) / 2
        for (w, c), tw in zip(line, widths):
            d.text((x, y), w, font=pfont, fill=WHITE if c is None else COLOURS[c % len(COLOURS)])
            x += tw + space
        y += lh
    box_y = (y + 45 if plines else top) // 2 * 2
    # the transparent hole with rounded corners
    mask = Image.new("L", (W, H), 255)
    ImageDraw.Draw(mask).rounded_rectangle((BOX_X, box_y, BOX_X + BOX_W - 1, box_y + bh - 1), radius=38, fill=0)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    y = box_y + bh + 35
    for line in clines:
        text = " ".join(w for w, _ in line)
        d.text(((W - d.textlength(text, font=cfont)) / 2, y), text, font=cfont, fill=WHITE)
        y += 48
    img.save(out)
    return {"x": BOX_X, "y": box_y, "w": BOX_W, "h": bh}


def _header(img, d, name, handle, avatar_path):
    from PIL import Image, ImageDraw
    nfont, hfont = _font(BOLD, 44), _font(BOLD, 28)
    size = 120
    text_w = max(d.textlength(name, font=nfont), d.textlength(handle, font=hfont))
    x0 = int((W - (size + 24 + text_w)) / 2)
    y0 = 50
    circle = Image.new("L", (size, size), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, size - 1, size - 1), fill=255)
    pic = None
    if avatar_path and Path(avatar_path).exists():
        try:
            pic = Image.open(avatar_path).convert("RGB")
            s = min(pic.size)
            pic = pic.crop(((pic.width - s) // 2, (pic.height - s) // 2, (pic.width + s) // 2, (pic.height + s) // 2))
            pic = pic.resize((size, size))
        except Exception:
            pic = None
    if pic is None:   # a letter avatar
        pic = Image.new("RGB", (size, size), (245, 245, 245))
        letter = (name or handle.lstrip("@") or "?")[0].upper()
        lf = _font(SERIF, 78)
        ld = ImageDraw.Draw(pic)
        ld.text((size / 2, size / 2), letter, font=lf, fill=(20, 20, 20), anchor="mm")
    img.paste(pic, (x0, y0), circle)
    d.ellipse((x0 - 2, y0 - 2, x0 + size + 1, y0 + size + 1), outline=(60, 60, 60), width=3)
    tx = x0 + size + 24
    if name and handle:
        d.text((tx, y0 + 22), name, font=nfont, fill=WHITE)
        d.text((tx, y0 + 76), handle, font=hfont, fill=GREY)
    else:
        d.text((tx, y0 + size / 2), name or handle, font=nfont, fill=WHITE, anchor="lm")
