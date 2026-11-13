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
    if fw >= W * 0.16:
        info["layout"] = "crop"      # the streamer fills the frame (just chatting / IRL)
        return info
    # Small face = webcam overlay. Grow the face box into a panel-shaped box around it.
    aspect = 1080 / TOP_H
    ch = min(H, fh * 2.8)
    cw = min(W, ch * aspect)
    ch = cw / aspect
    x = float(np.clip(cx - cw / 2, 0, W - cw))
    y = float(np.clip(cy - ch * 0.45, 0, H - ch))
    info["layout"] = "split"
    info["cam"] = {"x": int(x), "y": int(y), "w": int(cw) // 2 * 2, "h": int(ch) // 2 * 2}
    return info
