"""Turns a source clip into the finished 1080x1920 Short with ffmpeg."""
import os
from pathlib import Path

from . import captions, config
from .layout import BOT_H, TOP_H

DEFAULT_ZOOM = 73   # picture height as % of the screen in the bars/blurred layouts (about 1400 of 1920)


def _video_chain(lay: dict, layout: str, zoom=None) -> str:
    W, H = lay["width"], lay["height"]
    if layout == "split" and lay.get("cam"):
        c = lay["cam"]
        bw = min(W, int(H * 1080 / BOT_H) // 2 * 2)
        return (f"[0:v]split[c0][g0];"
                f"[c0]crop={c['w']}:{c['h']}:{c['x']}:{c['y']},scale=1080:{TOP_H}[cam];"
                f"[g0]crop={bw}:{H}:{(W - bw) // 2}:0,scale=1080:{BOT_H}[game];"
                f"[cam][game]vstack[v0]")
    if layout in ("bars", "blur"):
        return _framed(lay, picture_height(lay, zoom), layout)
    if layout == "crop":
        cw = min(W, int(H * 9 / 16) // 2 * 2)
        cx = lay.get("face_x") or W / 2
        x = int(max(0, min(W - cw, cx - cw / 2)))
        return f"[0:v]crop={cw}:{H}:{x}:0,scale=1080:1920[v0]"
    return _framed(lay, picture_height(lay, zoom), "blur")


def picture_height(lay: dict, zoom) -> int:
    """Height of the picture inside the 1920 frame for the black-bars and blurred layouts.
    zoom is that height as a % of the screen; the lowest setting shows the whole frame uncropped."""
    W, H = lay["width"], lay["height"]
    whole = int(1080 * H / W)                       # picture height with nothing cropped off the sides
    want = int(1920 * float(zoom or DEFAULT_ZOOM) / 100)
    return max(min(want, 1920), min(whole, 1920)) // 2 * 2


def _framed(lay: dict, ph: int, bg: str) -> str:
    """The picture (1080 wide, ph tall) in the middle of the frame, cropped around the face when one was
    found. Above and below it: black, or a blurred, darkened, zoomed-in copy of the same video."""
    W, H = lay["width"], lay["height"]
    a = 1080 / ph
    if W / H > a:
        cw, ch = int(H * a) // 2 * 2, H // 2 * 2
    else:
        cw, ch = W // 2 * 2, int(W / a) // 2 * 2
    cx = lay.get("face_x") or W / 2
    x = int(max(0, min(W - cw, cx - cw / 2)))
    y = (H - ch) // 2
    top = (1920 - ph) // 2
    pic = f"crop={cw}:{ch}:{x}:{y},scale=1080:{ph}"
    if bg != "blur" or ph >= 1920:
        return f"[0:v]{pic},pad=1080:1920:0:{top}:black[v0]"
    return ("[0:v]split[b0][f0];"
            "[b0]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:3,"
            "eq=brightness=-0.12[bg];"
            f"[f0]{pic}[fg];"
            f"[bg][fg]overlay=0:{top}[v0]")


def render(job_dir: Path, clip: dict, words: list, out: Path, music_path=None) -> None:
    """clip needs: src (file in job_dir), trim_start, trim_end, layout, layout_info, caption_style, highlight,
    hook, progress_bar, music_volume."""
    dur = clip["trim_end"] - clip["trim_start"]
    layout = clip["layout"]
    if layout == "split" and not clip["layout_info"].get("cam"):
        layout = "blur"

    picture = None
    if layout in ("bars", "blur"):
        ph = picture_height(clip["layout_info"], clip.get("zoom"))
        picture = ((1920 - ph) // 2, ph)            # where the picture sits, so captions can sit on it

    ass_name = f"{clip['id']}.ass"
    (job_dir / ass_name).write_text(
        captions.build(words, dur, clip["caption_style"], clip["highlight"], clip.get("hook", ""),
                       layout, offset=clip["trim_start"], meme_text=clip.get("context", ""), picture=picture),
        "utf-8")

    fc = [_video_chain(clip["layout_info"], layout, clip.get("zoom"))]
    last = "v0"
    if clip.get("caption_style") != "none" or clip.get("hook"):
        # The bundled caption font, given as a relative path because ffmpeg runs inside the job folder
        # and a Windows drive letter's colon would break the filter syntax.
        fonts = os.path.relpath(config.ROOT / "assets" / "fonts", job_dir).replace("\\", "/")
        fc.append(f"[{last}]subtitles={ass_name}:fontsdir={fonts}[v1]")
        last = "v1"
    if clip.get("progress_bar"):
        fc.append(f"color=c=0xFFF200:s=1080x12:d={dur:.3f}[bar];"
                  f"[{last}][bar]overlay=x='-W+W*t/{dur:.3f}':y=H-12:shortest=1[v2]")
        last = "v2"
    fc.append(f"[{last}]format=yuv420p[vout]")

    inputs = ["-ss", f"{clip['trim_start']:.3f}", "-t", f"{dur:.3f}", "-i", clip["src"]]
    voice = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    fade = f"afade=t=in:d=0.08,afade=t=out:st={max(0, dur - 0.25):.3f}:d=0.25"
    if music_path:
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]
        vol = float(clip.get("music_volume") or 0.18)
        fc.append(f"{voice},asplit[vc][sc];"
                  f"[1:a]aresample=48000,volume={vol:.3f},atrim=0:{dur:.3f}[m0];"
                  # the music dips whenever someone is talking
                  f"[m0][sc]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[m1];"
                  f"[vc][m1]amix=inputs=2:normalize=0:duration=first,{fade}[aout]")
    else:
        fc.append(f"{voice},{fade}[aout]")

    config.run_ffmpeg([
        *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
        "-c:v", *config.video_encoder(), *config.quality_args(21),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
        "-t", f"{dur:.3f}", str(out),
    ], cwd=str(job_dir), timeout=900)
