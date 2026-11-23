"""Alpha matte of a video: the person in white, the background in black.

Two methods, picked automatically:
  chroma  The clip was shot on a green screen (detected by sampling frames).
          FFmpeg chromakey + despill, keyed on the measured green.
  rvm     Anything else. Robust Video Matting (mobilenetv3) on this machine,
          frame by frame with its recurrent state, so edges stay stable.

Outputs go to <video_parent>/edit/mattes/ by default:
  <stem>_alpha.mp4   grayscale matte, same size, frame rate and length as the source
  <stem>_rgba.mov    with --rgba: ProRes 4444 with alpha (source + matte), audio kept

Usage:
    python helpers/matte.py <video>
    python helpers/matte.py <video> --rgba
    python helpers/matte.py <video> --mode rvm --downsample 0.25
    python helpers/matte.py <video> --mode chroma --similarity 0.15 --blend 0.05

Swap a background with the matte:
    ffmpeg -i fundo.mp4 -i video.mp4 -i edit/mattes/video_alpha.mp4 -filter_complex
      "[2:v]format=gray[a];[1:v][a]alphamerge[fg];[0:v][fg]overlay=shortest=1" out.mp4

Robust Video Matting lives outside this repo (default ~/RobustVideoMatting, with
weights/rvm_mobilenetv3.pth). Override with VIDEO_USE_RVM_DIR.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

RVM_DIR = Path(os.environ.get("VIDEO_USE_RVM_DIR", Path.home() / "RobustVideoMatting"))


# ---------------------------------------------------------------- probing

def probe(video: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames"
         ":stream_side_data=rotation:stream_tags=rotate:format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    st = data["streams"][0]
    w, h = int(st["width"]), int(st["height"])

    rot = 0
    for sd in st.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if not rot and "rotate" in (st.get("tags") or {}):
        rot = int(st["tags"]["rotate"])
    if abs(rot) % 180 == 90:  # phones store portrait as rotated landscape
        w, h = h, w

    rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "30/1"
    if rate in ("0/0", "0/1"):
        rate = st.get("r_frame_rate") or "30/1"
    fps = Fraction(rate)
    duration = float(data.get("format", {}).get("duration") or 0)
    return {"w": w, "h": h, "rate": rate, "fps": float(fps), "duration": duration}


# ---------------------------------------------------------------- green screen

def detect_green(video: Path, info: dict, samples: int = 8) -> tuple[bool, str, float]:
    """(is_green_screen, key_color_hex, green_fraction) from a few sampled frames."""
    sw = 160
    sh = max(2, round(sw * info["h"] / info["w"] / 2) * 2)
    step = max(1, int(info["fps"] * max(info["duration"], 1) / samples))
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video),
         "-vf", f"select='not(mod(n\\,{step}))',scale={sw}:{sh}",
         "-fps_mode", "passthrough", "-frames:v", str(samples),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    )
    px = np.frombuffer(out.stdout, np.uint8).reshape(-1, 3).astype(np.int32)
    if px.size == 0:
        return False, "0x00FF00", 0.0
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    green = (g > 80) & (g > r * 1.25) & (g > b * 1.25)
    frac = float(green.mean())
    if green.sum() == 0:
        return False, "0x00FF00", frac
    med = np.median(px[green], axis=0).astype(int)
    key = "0x{:02X}{:02X}{:02X}".format(*med)
    return frac >= 0.30, key, frac


def matte_chroma(video: Path, alpha_out: Path, key: str, similarity: float, blend: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-stats", "-i", str(video), "-an",
         "-vf", f"chromakey={key}:{similarity}:{blend},format=yuva420p,alphaextract,format=gray",
         "-c:v", "libx264", "-crf", "12", "-preset", "medium", "-pix_fmt", "yuv420p",
         str(alpha_out)],
        check=True,
    )


# ---------------------------------------------------------------- RVM

def load_rvm(variant: str):
    import torch

    weights = RVM_DIR / "weights" / f"rvm_{variant}.pth"
    if not (RVM_DIR / "model").is_dir() or not weights.exists():
        sys.exit(
            f"Robust Video Matting not found: need {RVM_DIR / 'model'} and {weights}.\n"
            "Clone https://github.com/PeterL1n/RobustVideoMatting there and download the "
            "weights from its v1.0.0 release, or set VIDEO_USE_RVM_DIR."
        )
    sys.path.insert(0, str(RVM_DIR))
    from model import MattingNetwork  # RVM's own package

    net = MattingNetwork(variant).eval()
    net.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    torch.set_num_threads(os.cpu_count() or 4)
    return net


def matte_rvm(video: Path, alpha_out: Path, info: dict, variant: str,
              downsample: float | None, batch: int = 4) -> None:
    import torch

    w, h = info["w"], info["h"]
    ratio = downsample or min(1.0, 512 / max(w, h))  # RVM's guidance: ~0.25 for HD
    net = load_rvm(variant)

    reader = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(video), "-an",
         "-fps_mode", "cfr", "-r", info["rate"],
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
    )
    writer = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-framerate", info["rate"],
         "-i", "-", "-c:v", "libx264", "-crf", "12", "-preset", "medium",
         "-pix_fmt", "yuv420p", str(alpha_out)],
        stdin=subprocess.PIPE,
    )

    frame_bytes = w * h * 3
    total = int(info["duration"] * info["fps"]) or None
    done, t0, last = 0, time.time(), 0.0
    rec = [None] * 4
    print(f"  rvm {variant}, {w}x{h}, downsample {ratio:.3f}", flush=True)

    with torch.inference_mode():
        while True:
            frames = []
            for _ in range(batch):
                buf = reader.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                frames.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
            if not frames:
                break
            src = torch.from_numpy(np.stack(frames)).permute(0, 3, 1, 2).float().div_(255)
            _fgr, pha, *rec = net(src.unsqueeze(0), *rec, downsample_ratio=ratio)
            alpha = (pha[0, :, 0].clamp(0, 1) * 255).round().to(torch.uint8).numpy()
            writer.stdin.write(alpha.tobytes())
            done += len(frames)
            now = time.time()
            if now - last > 2:
                fps = done / (now - t0)
                pct = f" {100 * done / total:.0f}%" if total else ""
                print(f"  {done} frames{pct}, {fps:.1f} fps", flush=True)
                last = now

    reader.stdout.close()
    reader.wait()
    writer.stdin.close()
    if writer.wait() != 0:
        sys.exit("ffmpeg failed writing the matte")
    print(f"  {done} frames in {time.time() - t0:.1f}s", flush=True)


# ---------------------------------------------------------------- RGBA

def make_rgba(video: Path, alpha: Path, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-stats", "-i", str(video), "-i", str(alpha),
         "-filter_complex", "[1:v]format=gray[a];[0:v][a]alphamerge,format=yuva444p10le[v]",
         "-map", "[v]", "-map", "0:a?", "-c:v", "prores_ks", "-profile:v", "4444",
         "-c:a", "pcm_s16le", "-shortest", str(out)],
        check=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Alpha matte of a video (RVM, or chromakey on green screen)")
    ap.add_argument("video", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, default=None,
                    help="Output directory (default: <video_parent>/edit/mattes)")
    ap.add_argument("--mode", choices=("auto", "rvm", "chroma"), default="auto")
    ap.add_argument("--rgba", action="store_true", help="Also write ProRes 4444 .mov with alpha")
    ap.add_argument("--variant", choices=("mobilenetv3", "resnet50"), default="mobilenetv3")
    ap.add_argument("--downsample", type=float, default=None,
                    help="RVM downsample ratio (default: auto, ~0.25 for 1080p)")
    ap.add_argument("--similarity", type=float, default=0.15, help="chromakey similarity")
    ap.add_argument("--blend", type=float, default=0.05, help="chromakey edge blend")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    out_dir = (args.out_dir or video.parent / "edit" / "mattes").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    alpha_out = out_dir / f"{video.stem}_alpha.mp4"

    info = probe(video)
    mode = args.mode
    is_green, key, frac = detect_green(video, info)
    if mode == "auto":
        mode = "chroma" if is_green else "rvm"
    print(f"{video.name}: {info['w']}x{info['h']} @ {info['fps']:.2f} fps, "
          f"green {frac:.0%} -> {mode}", flush=True)

    if mode == "chroma":
        print(f"  chromakey {key} similarity {args.similarity} blend {args.blend}", flush=True)
        matte_chroma(video, alpha_out, key, args.similarity, args.blend)
    else:
        matte_rvm(video, alpha_out, info, args.variant, args.downsample)
    print(f"  matte: {alpha_out}")

    if args.rgba:
        rgba_out = out_dir / f"{video.stem}_rgba.mov"
        make_rgba(video, alpha_out, rgba_out)
        print(f"  rgba:  {rgba_out}")


if __name__ == "__main__":
    main()
