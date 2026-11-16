"""Builds the .ass subtitle file: animated word-by-word captions plus the opening hook text."""
import re

HIGHLIGHTS = {  # ASS colours are &HBBGGRR
    "yellow": "&H00F2FF&", "green": "&H14FF39&", "cyan": "&HFFE500&",
    "pink": "&H9A3DFF&", "orange": "&H0A8CFF&", "white": "&HFFFFFF&",
}
STYLES = {
    # words per group, font size, uppercase, highlight the spoken word, pop-in animation
    "pop": dict(group=3, chars=16, size=96, upper=True, highlight=True, pop=True),
    "karaoke": dict(group=6, chars=30, size=70, upper=True, highlight=True, pop=False),
    "clean": dict(group=5, chars=30, size=62, upper=False, highlight=False, pop=False),
}

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,5,70,70,0,1
Style: Hook,Arial Black,60,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{hook_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).strip()


def _groups(words, max_words, max_chars):
    groups, cur = [], []
    for w in words:
        length = sum(len(x["text"]) + 1 for x in cur)
        gap = cur and w["start"] - cur[-1]["end"] > 0.6
        ended = cur and cur[-1]["text"][-1:] in ".!?"
        if cur and (len(cur) >= max_words or length + len(w["text"]) > max_chars or gap or ended):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


MEME_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Meme,Montserrat Black,76,&H00{bgr},&H00{bgr},&H00000000,&H50000000,-1,0,0,0,100,100,1,0,1,9,4,2,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _wrap(text: str, width: int = 20) -> list:
    """Splits text into lines of about `width` characters without breaking words."""
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def _meme(text: str, duration: float, highlight: str, layout: str, picture=None) -> str:
    """One context caption for the whole clip: bold, all capitals, coloured, thick black outline, *in asterisks*."""
    text = _clean(text).upper()
    if text and not text.startswith("*"):
        text = f"*{text}*"
    bgr = HIGHLIGHTS.get(highlight, HIGHLIGHTS["green"]).strip("&H")
    # Bottom edge of the text: low inside the picture for black bars, the lower third otherwise.
    if picture:
        top, ph = picture
        bottom = int(top + ph * 0.83)       # same spot as the example: low on the picture, not on the bar
    else:
        bottom = {"split": 760}.get(layout, 1480)
    out = MEME_HEADER.format(bgr=bgr)
    if text:
        body = "\\N".join(_wrap(text))
        out += (f"Dialogue: 0,{_ts(0)},{_ts(duration)},Meme,,0,0,0,,"
                f"{{\\an2\\pos(540,{bottom})\\fad(150,0)}}{body}\n")
    return out


EDIT_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Edit,Montserrat Black,98,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,6,3,5,60,60,0,1
Style: Hook,Montserrat Black,62,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,{hook_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
EDIT_COLOURS = ["&H14FF39&", "&H2A2AFF&", "&H00F2FF&"]     # green, red, yellow (BGR)
STOP = {"the", "and", "that", "this", "with", "just", "like", "what", "have", "your", "they", "there", "then",
        "were", "was", "for", "you", "are", "but", "not", "his", "her", "him", "she", "its", "it's", "i'm"}


def _keyword(group) -> int:
    """Index of the word to colour in a caption chunk: the longest word that isn't filler."""
    best, score = 0, -1
    for i, w in enumerate(group):
        t = re.sub(r"[^\w']", "", w["text"]).lower()
        sc = len(t) - (10 if t in STOP else 0) + (3 if w["text"].endswith(("!", "?")) else 0)
        if sc > score:
            best, score = i, sc
    return best


def _edit(words, duration, highlight, hook, layout, picture) -> str:
    """Edit-page captions: 1-3 words in the middle of the picture, revealed word by word, one keyword per
    chunk coloured (rotating green / red / yellow) with a soft glow."""
    if picture:
        top, ph = picture
        y = int(top + ph * 0.42)
        hook_y = max(60, top - 150) if top >= 200 else top + 40
    else:
        y = {"split": 690}.get(layout, 860)
        hook_y = {"split": 110}.get(layout, 250)
    first = HIGHLIGHTS.get(highlight, EDIT_COLOURS[0])
    colours = [first] + [c for c in EDIT_COLOURS if c != first]
    lines = [EDIT_HEADER.format(hook_y=hook_y)]
    hook = _clean(hook)
    if hook:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(min(3.0, duration))},Hook,,0,0,0,,{{\\fad(120,300)}}{hook.upper()}")
    groups = _groups(words, 3, 14)
    for gi, g in enumerate(groups):
        g_end = min(g[-1]["end"] + 0.3, groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration)
        key = _keyword(g) if len(g) > 1 or len(g[0]["text"]) >= 4 else -1
        col = colours[gi % len(colours)]
        for wi, w in enumerate(g):
            start = w["start"]
            end = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
            if end - start < 0.02:
                continue
            main, glow = [], []
            for j, x in enumerate(g):
                t = x["text"].upper()
                hidden = j > wi           # not spoken yet: keep its space so the line doesn't jump
                if hidden:
                    main.append(f"{{\\alpha&HFF&}}{t}{{\\alpha&H00&}}")
                    glow.append(t)
                    continue
                pop = "\\fscx125\\fscy125\\t(0,110,\\fscx100\\fscy100)" if j == wi else ""
                if j == key:
                    main.append(f"{{\\c{col}{pop}}}{t}{{\\c&HFFFFFF&\\fscx100\\fscy100}}")
                    glow.append(f"{{\\3a&H40&{pop}}}{t}{{\\3a&HFF&\\fscx100\\fscy100}}")
                else:
                    main.append(f"{{{pop}}}{t}{{\\fscx100\\fscy100}}" if pop else t)
                    glow.append(t)
            pos = f"\\pos(540,{y})"
            # Glow layer: invisible text with a wide, blurred, coloured outline only behind the keyword.
            if key >= 0 and key <= wi:
                lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Edit,,0,0,0,,"
                             f"{{{pos}\\1a&HFF&\\3a&HFF&\\4a&HFF&\\3c{col}\\bord16\\blur12}}{' '.join(glow)}")
            lines.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Edit,,0,0,0,,{{{pos}}}{' '.join(main)}")
    return "\n".join(lines) + "\n"


def build(words, duration, style="pop", highlight="yellow", hook="", layout="blur", offset=0.0,
          meme_text="", picture=None) -> str:
    """`words` are timed against the source clip; `offset` is where the final cut starts inside it."""
    if style == "meme":
        return _meme(meme_text or hook, duration, highlight, layout, picture)
    if style == "edit":
        return _edit(_shift(words, offset, duration), duration, highlight, hook, layout, picture)
    st = STYLES.get(style, STYLES["pop"])
    colour = HIGHLIGHTS.get(highlight, HIGHLIGHTS["yellow"])
    # split: on the seam between panels; blur: in the blurred band under the picture; crop: lower third
    cap_y = {"split": 690}.get(layout, 1320)
    hook_y = {"split": 110}.get(layout, 250)
    if picture:
        top, ph = picture
        cap_y = int(top + ph * 0.78)                 # lower part of the picture
        hook_y = max(60, top - 150) if top >= 200 else top + 40   # on the top bar when there's room
    lines = [HEADER.format(size=st["size"], hook_y=hook_y)]

    hook = _clean(hook)
    if hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(min(3.5, duration))},Hook,,0,0,0,,{{\\fad(120,300)}}{hook.upper()}")

    shifted = [] if style == "none" else _shift(words, offset, duration)

    groups = _groups(shifted, st["group"], st["chars"])
    for gi, g in enumerate(groups):
        g_end = min(g[-1]["end"] + 0.25, groups[gi + 1][0]["start"] if gi + 1 < len(groups) else duration)
        for wi, w in enumerate(g):
            start = w["start"]
            end = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
            if end - start < 0.02:
                continue
            parts = []
            for j, x in enumerate(g):
                t = x["text"].upper() if st["upper"] else x["text"]
                if st["highlight"] and j == wi:
                    grow = ""
                    if st["pop"]:   # the spoken word swells; the first word of a group pops in from small
                        frm = 70 if wi == 0 else 100
                        grow = f"\\fscx{frm}\\fscy{frm}\\t(0,90,\\fscx112\\fscy112)"
                    parts.append(f"{{\\c{colour}{grow}}}{t}{{\\c&HFFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            pre = f"\\pos(540,{cap_y})"
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{{pre}}}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def _shift(words, offset, duration):
    """Words moved onto the final cut's timeline, dropping any outside it."""
    out = []
    for w in words:
        s, e = w["start"] - offset, w["end"] - offset
        if e <= 0 or s >= duration:
            continue
        text = _clean(w["text"])
        if text:
            out.append({"start": max(0.0, s), "end": min(duration, e), "text": text})
    return out
