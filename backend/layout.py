"""Decide how a 16:9 clip should fill a 9:16 frame by looking for the streamer's face."""
import cv2
import numpy as np

TOP_H, BOT_H = 672, 1248   # split layout: facecam panel over gameplay panel (sums to 1920)


def detect(video_path) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    info = {"layout": "blur", "width": W, "height": H, "cam": None, "face_x": None}
    if not W or not H or total < 2:
        cap.release()
        return info
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    samples, faces = 12, []
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                         minSize=(int(H * 0.05), int(H * 0.05)))
        if len(found):
            faces.append(max(found, key=lambda f: f[2] * f[3]))   # biggest face in the frame
    cap.release()
    if len(faces) < samples * 0.4:
        return info
    f = np.array(faces, dtype=float)
    cx, cy = np.median(f[:, 0] + f[:, 2] / 2), np.median(f[:, 1] + f[:, 3] / 2)
    # Keep only detections that agree on where the face is; a webcam overlay doesn't move.
    near = f[(np.abs(f[:, 0] + f[:, 2] / 2 - cx) < W * 0.08) & (np.abs(f[:, 1] + f[:, 3] / 2 - cy) < H * 0.1)]
    if len(near) < samples * 0.3:
        return info
    fw, fh = float(np.median(near[:, 2])), float(np.median(near[:, 3]))
    info["face_x"] = float(cx)
    # A webcam overlay is small AND tucked into a corner of the frame. A face anywhere else is just the person
    # in the shot (IRL, podcast, vlog), and splitting would show the same scene twice.
    in_corner = (min(cx, W - cx) < W * 0.3) and (min(cy, H - cy) < H * 0.35)
    if fw >= W * 0.16 or not in_corner:
        info["layout"] = "crop"
        return info
    # Small face in a corner = webcam overlay. Grow the face box into a panel-shaped box around it.
    aspect = 1080 / TOP_H
    ch = min(H, fh * 2.8)
    cw = min(W, ch * aspect)
    ch = cw / aspect
    x = float(np.clip(cx - cw / 2, 0, W - cw))
    y = float(np.clip(cy - ch * 0.45, 0, H - ch))
    info["layout"] = "split"
    info["cam"] = {"x": int(x), "y": int(y), "w": int(cw) // 2 * 2, "h": int(ch) // 2 * 2}
    return info


def track(video_path, start: float, dur: float, step: float = 0.4) -> list:
    """Follows the main face across [start, start+dur] of the file. Returns [(t, cx), ...] with t from `start`,
    smoothed and reduced to the points where the motion changes, or [] when no face is found often enough."""
    import subprocess
    from . import config
    cap = cv2.VideoCapture(str(video_path))
    W, H = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if not W or not H:
        return []
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    sw = min(960, W) // 2 * 2
    sh = int(H * sw / W) // 2 * 2
    scale = sw / W
    # ffmpeg decodes straight through and hands over small grey frames: much faster than seeking with OpenCV
    p = subprocess.run([config.ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                        "-i", str(video_path), "-vf", f"fps={1 / step:g},scale={sw}:{sh}", "-f", "rawvideo",
                        "-pix_fmt", "gray", "-"], capture_output=True, creationflags=config.NO_WINDOW, timeout=600)
    frames = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(frames) // (sw * sh)
    times, xs = [], []
    for i in range(n):
        gray = frames[i * sw * sh:(i + 1) * sw * sh].reshape(sh, sw)
        found = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(int(sh * 0.06), int(sh * 0.06)))
        times.append(i * step)
        xs.append((max(found, key=lambda f: f[2] * f[3])[0] + max(found, key=lambda f: f[2] * f[3])[2] / 2) / scale
                  if len(found) else np.nan)
    x = np.array(xs, dtype=float)
    if len(x) < 3 or np.isnan(x).mean() > 0.6:
        return []
    # fill gaps by holding the nearest detection, drop one-frame jumps, then smooth
    idx = np.arange(len(x))
    good = ~np.isnan(x)
    x = np.interp(idx, idx[good], x[good])
    from scipy.ndimage import median_filter, uniform_filter1d
    x = median_filter(x, size=7, mode="nearest")
    # Camera-operator smoothing: hold still while the face stays near the centre of the frame, and pan at a
    # limited speed when it moves away, like a person reframing rather than a jittery lock-on.
    dead, speed = W * 0.07, W * 0.18 * step
    pos, out = float(x[0]), []
    for v in x:
        gap = v - pos
        if abs(gap) > dead:
            pos += float(np.clip(gap - np.sign(gap) * dead, -speed, speed))
        out.append(pos)
    x = uniform_filter1d(np.array(out), size=3, mode="nearest")
    if float(x.max() - x.min()) < W * 0.04:          # barely moves: a fixed crop is steadier
        return [(0.0, float(np.median(x)))]
    pts = _simplify(list(zip(times, x.tolist())), W * 0.012)
    return [(round(a, 2), round(b, 1)) for a, b in pts]


def _simplify(pts, tol):
    """Ramer-Douglas-Peucker on (t, x) so the crop moves in a few smooth straight pans."""
    if len(pts) < 3:
        return pts
    (t0, x0), (t1, x1) = pts[0], pts[-1]
    worst, wi = 0.0, 0
    for i in range(1, len(pts) - 1):
        t, x = pts[i]
        d = abs(x - (x0 + (x1 - x0) * (t - t0) / max(1e-6, t1 - t0)))
        if d > worst:
            worst, wi = d, i
    if worst <= tol:
        return [pts[0], pts[-1]]
    return _simplify(pts[: wi + 1], tol)[:-1] + _simplify(pts[wi:], tol)


def x_expr(points: list, crop_w: int, W: int) -> str:
    """ffmpeg expression for a crop's x that follows the tracked face (t = seconds from the clip start)."""
    lo, hi = 0, W - crop_w
    if not points:
        return str(max(lo, min(hi, int((W - crop_w) / 2))))
    if len(points) == 1:
        return str(int(max(lo, min(hi, points[0][1] - crop_w / 2))))
    terms = []
    for (t0, x0), (t1, x1) in zip(points, points[1:]):
        terms.append(f"between(t,{t0:.2f},{t1:.2f}-0.001)*({x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{max(0.01, t1 - t0):.2f})")
    first, last = points[0][1], points[-1][1]
    body = "+".join(terms) + f"+lt(t,{points[0][0]:.2f})*{first:.1f}+gte(t,{points[-1][0]:.2f})*{last:.1f}"
    return f"max({lo},min({hi},({body})-{crop_w / 2:.1f}))"
