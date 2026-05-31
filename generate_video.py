"""
早安电台视频生成器
- TTS: edge-tts (zh-CN-XiaoxiaoNeural — 知性女声)
- 画面: Pexels 免费素材 / 程序化春日动画
- 滤镜: 春日暖色调
- 输出: MP4 带字幕
"""
import asyncio
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
)
from PIL import Image as PILImage, ImageDraw, ImageFont

# ── 配置 ───────────────────────────────────────────────────────
ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
LOCAL_STOCK = ROOT / "素材库"
LOCAL_STOCK.mkdir(exist_ok=True)
PHOTO_STOCK = LOCAL_STOCK / "photos"
PHOTO_STOCK.mkdir(exist_ok=True)
PEXELS_KEY = os.environ.get("PEXELS_KEY", "")
W, H = 1920, 1080


# ═══════════════════════════════════════════════════════════════
# 1. TTS
# ═══════════════════════════════════════════════════════════════
async def _tts(text, out, voice="zh-CN-XiaoxiaoNeural"):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate="+3%", pitch="+2Hz")
    await comm.save(out)
    return out


def tts(text, out):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_tts(text, out))
    finally:
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


# ═══════════════════════════════════════════════════════════════
# 2. 素材获取
# ═══════════════════════════════════════════════════════════════
def pexels_search(query, per_page=10):
    headers = {"Authorization": f"Bearer {PEXELS_KEY}"}
    url = f"https://api.pexels.com/videos/search?query={query}&per_page={per_page}&orientation=landscape&size=medium"
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            videos = []
            for v in r.json().get("videos", []):
                best = None
                for f in v.get("video_files", []):
                    w = f.get("width", 0)
                    if 1280 <= w <= 1920:
                        if best is None or w > best.get("width", 0):
                            best = {"url": f["link"], "w": w, "h": f.get("height", 0),
                                    "dur": v.get("duration", 10)}
                if best:
                    videos.append(best)
            return videos
        else:
            print(f"  Pexels returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  Pexels search: {e}")
    return []


def download(url, out):
    try:
        r = requests.get(url, stream=True, timeout=60)
        if r.status_code == 200:
            with open(out, "wb") as f:
                for c in r.iter_content(8192):
                    f.write(c)
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
# 3. 程序化场景（6种清晨画面）
# ═══════════════════════════════════════════════════════════════

# ── 天空渐变色板 ──
_SKY_PALETTES = [
    # (top_r, top_g, top_b, bot_r, bot_g, bot_b, name)
    (255, 180, 160, 200, 220, 200, "暖桃色晨曦"),
    (220, 200, 240, 180, 200, 210, "淡紫色薄雾"),
    (255, 220, 180, 220, 230, 200, "金色清晨"),
    (200, 210, 240, 210, 220, 200, "清冷蓝调"),
    (255, 200, 170, 190, 210, 190, "橘粉霞光"),
    (240, 210, 200, 200, 215, 195, "柔粉春日"),
]

def _sky_gradient(img, rng, pal_idx):
    """Draw sky gradient from palette."""
    h, w = img.shape[:2]
    top_r, top_g, top_b, bot_r, bot_g, bot_b, _ = _SKY_PALETTES[pal_idx % len(_SKY_PALETTES)]
    for y in range(h):
        t = y / h
        rr = int(top_r * (1 - t) + bot_r * t + rng.randint(-3, 3))
        gg = int(top_g * (1 - t) + bot_g * t + rng.randint(-3, 3))
        bb = int(top_b * (1 - t) + bot_b * t + rng.randint(-3, 3))
        img[y, :] = [np.clip(rr, 0, 255), np.clip(gg, 0, 255), np.clip(bb, 0, 255)]


def _draw_particles(img, rng, colors, count=80, min_sz=2, max_sz=12):
    """Draw circular particles (petals, leaves, light spots)."""
    h, w = img.shape[:2]
    for _ in range(count):
        px = rng.randint(0, w - 1)
        py = rng.randint(0, h - 1)
        sz = rng.randint(min_sz, max_sz + 1)
        alpha = rng.uniform(0.25, 0.65)
        color = rng.choice(colors)
        for dy in range(-sz, sz + 1):
            for dx in range(-sz, sz + 1):
                if dx * dx + dy * dy <= sz * sz:
                    ny, nx = py + dy, px + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        img[ny, nx] = [int(img[ny, nx, c] * (1 - alpha) + color[c] * alpha) for c in range(3)]


def _draw_buildings(img, rng, horizon):
    """Draw simple building silhouettes at horizon."""
    h, w = img.shape[:2]
    ground_y = int(h * horizon)
    # Ground
    img[ground_y:, :] = img[ground_y:, :] * [0.65, 0.75, 0.55]
    # Buildings
    n_bld = rng.randint(5, 12)
    x = 0
    for _ in range(n_bld):
        bw = rng.randint(40, 180)
        bh = rng.randint(60, int(h * (1 - horizon) * 0.85))
        bx = min(x, w - bw)
        roof_color = [
            rng.randint(60, 110),
            rng.randint(55, 100),
            rng.randint(50, 90),
        ]
        for dy in range(bh):
            for dx in range(bw):
                if 0 <= bx + dx < w and ground_y - bh + dy >= 0:
                    shade = 0.6 + 0.4 * (dy / bh)
                    for c in range(3):
                        img[ground_y - bh + dy, bx + dx, c] = int(
                            img[ground_y - bh + dy, bx + dx, c] * 0.3 +
                            roof_color[c] * shade * 0.7
                        )
        # Windows
        for wy in range(rng.randint(1, 3)):
            for wx in range(rng.randint(1, 3)):
                win_x = bx + rng.randint(8, bw - 18)
                win_y = ground_y - bh + rng.randint(10, bh - 20)
                win_sz = rng.randint(3, 7)
                for dy in range(win_sz):
                    for dx in range(win_sz):
                        ny, nx = win_y + dy, win_x + dx
                        if 0 <= ny < h and 0 <= nx < w:
                            img[ny, nx] = [rng.randint(220, 255) for _ in range(3)]
        x += bw + rng.randint(5, 30)
        if x >= w:
            break


def _draw_trees(img, rng, horizon):
    """Draw simple tree shapes."""
    h, w = img.shape[:2]
    ground_y = int(h * horizon)
    n_trees = rng.randint(3, 8)
    for _ in range(n_trees):
        tx = rng.randint(20, w - 20)
        tree_h = rng.randint(80, 180)
        trunk_w = rng.randint(4, 10)
        # Trunk
        trunk_color = [rng.randint(60, 100), rng.randint(40, 70), rng.randint(30, 50)]
        for dy in range(tree_h // 3):
            y = ground_y - dy
            for dx in range(trunk_w):
                x = tx - trunk_w // 2 + dx
                if 0 <= x < w and y >= 0:
                    img[y, x] = trunk_color
        # Canopy (circle of leaves)
        canopy_y = ground_y - tree_h // 3
        canopy_r = tree_h // 3
        leaf_colors = [
            [rng.randint(100, 200), rng.randint(140, 220), rng.randint(80, 160)],
            [rng.randint(120, 210), rng.randint(150, 230), rng.randint(100, 170)],
            [rng.randint(160, 230), rng.randint(170, 240), rng.randint(130, 200)],
        ]
        for dy in range(-canopy_r, canopy_r + 1):
            for dx in range(-canopy_r, canopy_r + 1):
                if dx * dx + dy * dy <= canopy_r * canopy_r:
                    ny, nx = canopy_y + dy, tx + dx
                    if 0 <= ny < h and 0 <= nx < w:
                        lc = rng.choice(leaf_colors)
                        img[ny, nx] = lc


def _draw_light_rays(img, rng, count=8):
    """Draw soft diagonal light rays from upper-left."""
    h, w = img.shape[:2]
    for _ in range(count):
        sx = rng.randint(0, w // 3)
        sy = rng.randint(0, h // 4)
        ray_len = rng.randint(200, 600)
        ray_w = rng.randint(30, 80)
        angle = rng.uniform(0.3, 0.7)
        for step in range(ray_len):
            t = step / ray_len
            alpha = max(0, (1 - t) * 0.08)
            rx = int(sx + step * np.cos(angle))
            ry = int(sy + step * np.sin(angle))
            for dw in range(ray_w // 2):
                for sign in (-1, 1):
                    px = rx + sign * dw
                    py = ry
                    if 0 <= py < h and 0 <= px < w:
                        img[py, px] = [int(img[py, px, c] * (1 - alpha) + 255 * alpha) for c in range(3)]


def scene_frame(w, h, scene_type, seed=0):
    """Generate a procedural morning scene frame."""
    rng = random.Random(seed)
    img = np.zeros((h, w, 3), dtype=np.uint8)

    pal_idx = scene_type % len(_SKY_PALETTES)
    _sky_gradient(img, rng, pal_idx)

    if scene_type == 0:
        # Cherry blossom — petals + trees
        _draw_trees(img, rng, 0.58)
        _draw_particles(img, rng, [
            [255, 200, 210], [255, 220, 230], [255, 180, 200], [255, 240, 245]
        ], count=rng.randint(80, 150), min_sz=3, max_sz=14)
        _draw_light_rays(img, rng, 6)

    elif scene_type == 1:
        # Morning street — buildings + warm light
        _draw_buildings(img, rng, 0.55)
        _draw_light_rays(img, rng, 12)
        _draw_particles(img, rng, [
            [255, 240, 200], [255, 250, 220], [255, 230, 180]
        ], count=40, min_sz=1, max_sz=5)

    elif scene_type == 2:
        # Park path — trees + dappled light
        _draw_trees(img, rng, 0.62)
        _draw_particles(img, rng, [
            [255, 245, 200], [255, 255, 220], [200, 240, 200],
            [220, 255, 180], [180, 220, 160],
        ], count=rng.randint(60, 100), min_sz=2, max_sz=10)
        _draw_light_rays(img, rng, 8)

    elif scene_type == 3:
        # Village sunrise — horizon buildings
        _draw_buildings(img, rng, 0.65)
        _draw_particles(img, rng, [
            [255, 200, 150], [255, 220, 180], [255, 180, 140]
        ], count=50, min_sz=1, max_sz=6)
        _draw_light_rays(img, rng, 15)

    elif scene_type == 4:
        # Warm coffee / intimate — soft close-up feel
        _draw_particles(img, rng, [
            [255, 230, 200], [255, 240, 210], [200, 180, 160],
            [240, 200, 180], [255, 220, 190],
        ], count=rng.randint(100, 160), min_sz=4, max_sz=20)
        _draw_light_rays(img, rng, 4)

    else:
        # River reflection — horizontal soft bands + sparkles
        for y in range(h):
            t = y / h
            ripple = np.sin(y * 0.3 + rng.randint(0, 100) * 0.01) * 0.03
            t2 = max(0, min(1, t + ripple))
            for c in range(3):
                img[y, :, c] = np.clip(img[y, :, c].astype(float) * (0.9 + 0.1 * t2), 0, 255).astype(np.uint8)
        _draw_particles(img, rng, [
            [255, 255, 240], [200, 230, 255], [220, 240, 255]
        ], count=60, min_sz=1, max_sz=4)

    return img


def scene_clip(dur, scene_type):
    """Create a procedural scene clip with Ken Burns zoom."""
    def make_frame(t):
        s = int(t * 24)
        return scene_frame(W, H, scene_type, s)

    clip = VideoClip(make_frame, duration=dur)
    clip.fps = 24

    def zoom(get_frame, t):
        frame = get_frame(t)
        progress = t / dur if dur > 0 else 0
        scale = 1.0 + progress * 0.06
        pil = PILImage.fromarray(frame)
        nw, nh = int(W * scale), int(H * scale)
        pil = pil.resize((nw, nh), PILImage.LANCZOS)
        left, top = (nw - W) // 2, (nh - H) // 2
        return np.array(pil.crop((left, top, left + W, top + H)))

    return clip.fl(zoom)


# ═══════════════════════════════════════════════════════════════
# 3.5 照片 Ken Burns 效果
# ═══════════════════════════════════════════════════════════════
def photo_clip(photo_path, dur):
    """Create a Ken Burns video clip from a still photo."""
    pil_img = PILImage.open(photo_path).convert("RGB")
    pw, ph = pil_img.size

    # Resize to at least fill screen
    scale = max(W / pw, H / ph)
    nw, nh = int(pw * scale), int(ph * scale)
    pil_img = pil_img.resize((nw, nh), PILImage.LANCZOS)

    # Pick a random pan direction for variety
    directions = [
        (0, 0, 1, 1),       # top-left to bottom-right
        (1, 0, 0, 1),       # top-right to bottom-left
        (0.5, 0, 0.5, 1),   # center-top to center-bottom
        (0, 0.5, 1, 0.5),   # left-center to right-center
        (0.5, 0.5, 0.5, 0.5),  # center (zoom only, no pan)
    ]
    import random as _rnd
    sx, sy, ex, ey = _rnd.choice(directions)

    def make_frame(t):
        progress = t / dur if dur > 0 else 0
        # Ease in-out
        p = progress * progress * (3 - 2 * progress)
        # Zoom: start at 1.0, end at 1.08
        scale_t = 1.0 + p * 0.08
        # Pan position
        cx = int((sx + (ex - sx) * p) * nw)
        cy = int((sy + (ey - sy) * p) * nh)
        # Apply zoom
        sw, sh = int(W / scale_t), int(H / scale_t)
        left = max(0, min(cx - sw // 2, nw - sw))
        top = max(0, min(cy - sh // 2, nh - sh))
        crop = pil_img.crop((left, top, left + sw, top + sh))
        crop = crop.resize((W, H), PILImage.LANCZOS)
        return np.array(crop)

    clip = VideoClip(make_frame, duration=dur)
    clip.fps = 24
    return clip


# ═══════════════════════════════════════════════════════════════
# 4. 字幕 (PIL 渲染, 不依赖 ImageMagick)
# ═══════════════════════════════════════════════════════════════

# Font paths
_FONT_CACHE = {}


def _get_font(name, size):
    key = (name, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    # Try common font paths
    paths = [
        f"C:\\Windows\\Fonts\\{name}.ttf",
        f"C:\\Windows\\Fonts\\{name}.ttc",
        f"C:\\Windows\\Fonts\\msyh.ttc",
        f"C:\\Windows\\Fonts\\simhei.ttf",
        f"C:\\Windows\\Fonts\\simsun.ttc",
    ]
    for p in paths:
        if Path(p).exists():
            try:
                font = ImageFont.truetype(p, size)
                _FONT_CACHE[key] = font
                return font
            except Exception:
                pass
    # Fallback: default font
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _pil_text_image(text, fontname, fontsize, color, stroke_color, stroke_width, max_width, glass_bg=False):
    """Render text to a transparent RGBA PIL image with optional glass background."""
    font = _get_font(fontname, fontsize)

    # Calculate wrapping
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph.strip():
            lines.append(' ')
            continue
        current = ''
        for ch in paragraph:
            test = current + ch
            bbox = font.getbbox(test)
            if bbox[2] - bbox[0] > max_width and current:
                lines.append(current)
                current = ch
            else:
                current = test
        if current:
            lines.append(current)

    line_height = int(fontsize * 1.5)
    pad = stroke_width * 4 + 20
    img_h = line_height * len(lines) + pad
    img_w = max_width + pad

    img = PILImage.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Glass background (rounded rectangle)
    if glass_bg:
        glass_pad = 12
        gx1, gy1 = glass_pad, glass_pad
        gx2, gy2 = img_w - glass_pad, img_h - glass_pad
        # Draw rounded rect with semi-transparent black
        r = 16
        draw.rounded_rectangle([gx1, gy1, gx2, gy2], radius=r,
                               fill=(0, 0, 0, 100), outline=(255, 255, 255, 40), width=1)

    y = stroke_width * 2 + 10
    for line in lines:
        bbox = font.getbbox(line)
        tw = bbox[2] - bbox[0]
        x = (img_w - tw) // 2

        if stroke_width > 0:
            for dx in range(-stroke_width, stroke_width + 1):
                for dy in range(-stroke_width, stroke_width + 1):
                    if dx * dx + dy * dy <= stroke_width * stroke_width:
                        draw.text((x + dx, y + dy), line, font=font, fill=stroke_color)

        draw.text((x, y), line, font=font, fill=color)
        y += line_height

    img = img.crop(img.getbbox() or (0, 0, img_w, img_h))
    return np.array(img)


def make_text_clip(text, fontsize=40, color="white", stroke_color="black",
                   stroke_width=2, max_width=None, fontname="msyh", glass_bg=False):
    """Create an ImageClip from PIL-rendered text. Use in CompositeVideoClip."""
    if max_width is None:
        max_width = int(W * 0.85)
    img_array = _pil_text_image(text, fontname, fontsize, color, stroke_color, stroke_width, max_width, glass_bg)
    clip = ImageClip(img_array, transparent=True)
    return clip


def subtitle_clips(text, audio_dur):
    """Split text into sentence subtitle clips with glass background."""
    sents = [s.strip() for s in re.split(r"[。！？\n]+", text) if len(s.strip()) > 2]
    if not sents:
        return []
    clips = []
    t = 0.8
    cps = 3.5
    for sent in sents:
        dur = max(1.5, min(len(sent) / cps, audio_dur - t))
        if dur < 0.5:
            break
        try:
            tc = make_text_clip(sent, fontsize=42, color="#FFFEF0",
                                stroke_color="#222222", stroke_width=2, glass_bg=True)
            tc = tc.set_position(("center", int(H * 0.80)))
            tc = tc.set_start(t).set_duration(dur)
            tc = tc.crossfadein(0.25)
            clips.append(tc)
        except Exception as e:
            print(f"    subtitle warn: {e}")
        t += dur
    return clips


# ═══════════════════════════════════════════════════════════════
# 5. 主流程
# ═══════════════════════════════════════════════════════════════
def build_video(script_text, output_name="morning_radio"):
    print("=" * 50)
    print("  早安电台视频生成")
    print("=" * 50)
    tmp = Path(tempfile.mkdtemp(prefix="radio_"))

    # ── TTS ─────────────────────────────────────────────────
    print("\n[1/4] TTS 语音 (zh-CN-XiaoxiaoNeural 知性女声)...")
    audio_path = tmp / "voice.mp3"
    tts(script_text, str(audio_path))
    audio = AudioFileClip(str(audio_path))
    total_dur = audio.duration
    print(f"  语音时长: {total_dur:.0f}s")

    # ── 素材 ────────────────────────────────────────────────
    print("\n[2/4] 获取视频素材...")

    # 1) 本地素材库
    local_videos = list(LOCAL_STOCK.glob("*.mp4"))
    print(f"  本地素材库: {len(local_videos)} 个")

    # 2) Pexels 在线搜索（API Key 可能受限，快速尝试）
    queries = [
        "street morning", "city sunrise", "park spring",
        "flower blossom", "sunlight morning",
    ]
    all_vids = []
    random.shuffle(queries)
    for q in queries[:2]:  # 只试2个关键词，快速降级
        vids = pexels_search(q, 6)
        all_vids.extend(vids)
        print(f"  Pexels '{q}' → {len(vids)} 个")
        if len(all_vids) >= 8:
            break
        time.sleep(0.8)

    # 去重
    seen = set()
    unique = []
    for v in all_vids:
        if v["url"] not in seen:
            seen.add(v["url"])
            unique.append(v)
    all_vids = unique

    clips = []

    # 1) 本地视频素材
    if local_videos:
        print(f"  加载本地视频素材...")
        random.shuffle(local_videos)
        for vp in local_videos[:6]:
            try:
                vc = VideoFileClip(str(vp)).without_audio()
                vw, vh = vc.size
                scale = max(W / vw, H / vh)
                vc = vc.resize(scale)
                vw2, vh2 = vc.size
                xc, yc = (vw2 - W) // 2, (vh2 - H) // 2
                vc = vc.crop(x1=xc, y1=yc, x2=xc + W, y2=yc + H)
                vc = vc.set_duration(min(vc.duration, 15))
                clips.append(vc)
                print(f"    OK 本地视频: {vp.name}")
            except Exception as e:
                print(f"    FAIL 本地视频 {vp.name}: {e}")

    # 2) 本地照片素材 (Ken Burns 效果)
    local_photos = list(PHOTO_STOCK.glob("*.jpg")) + list(PHOTO_STOCK.glob("*.jpeg")) + list(PHOTO_STOCK.glob("*.png"))
    if local_photos and len(clips) < 5:
        print(f"  加载本地照片素材 ({len(local_photos)} 张)...")
        random.shuffle(local_photos)
        for pp in local_photos[:5]:
            try:
                d = min(10.0, total_dur * 0.4)
                pc = photo_clip(str(pp), d)
                clips.append(pc)
                print(f"    OK 照片 Ken Burns: {pp.name}")
            except Exception as e:
                print(f"    FAIL 照片 {pp.name}: {e}")

    # 3) Pexels 在线视频
    need = max(0, 5 - len(clips))
    if all_vids and need > 0:
        print(f"  下载在线素材 (需要 {need} 个)...")
        random.shuffle(all_vids)
        n = 0
        for v in all_vids:
            if n >= need:
                break
            # 下载到本地素材库（缓存复用）
            idx = len(list(LOCAL_STOCK.glob("pexels_*.mp4")))
            vp = LOCAL_STOCK / f"pexels_{idx:04d}.mp4"
            ok = download(v["url"], str(vp))
            if not ok:
                vp = tmp / f"s{n}.mp4"
                ok = download(v["url"], str(vp))
            if ok:
                try:
                    vc = VideoFileClip(str(vp)).without_audio()
                    vw, vh = vc.size
                    scale = max(W / vw, H / vh)
                    vc = vc.resize(scale)
                    vw2, vh2 = vc.size
                    xc, yc = (vw2 - W) // 2, (vh2 - H) // 2
                    vc = vc.crop(x1=xc, y1=yc, x2=xc + W, y2=yc + H)
                    vc = vc.set_duration(min(vc.duration, 12))
                    clips.append(vc)
                    n += 1
                    print(f"    OK 素材 {n}")
                except Exception as e:
                    print(f"    FAIL: {e}")

    if not clips:
        print("  素材不可用, 生成春日画面...")
        remain = total_dur
        # 6种场景轮换
        st = 0
        while remain > 0:
            d = min(6.0, remain)
            clips.append(scene_clip(d, st))
            st = (st + 1) % 6
            remain -= d
        print(f"  程序化生成了 {len(clips)} 段（6种场景轮换）")

    # 为所有素材片段添加淡入淡出
    for i, c in enumerate(clips):
        if c.duration > 1.5:
            clips[i] = c.crossfadein(0.6).crossfadeout(0.6)
        elif c.duration > 0.8:
            clips[i] = c.crossfadein(0.25).crossfadeout(0.25)

    # 拼接
    if len(clips) == 1:
        video = clips[0].set_duration(total_dur)
    else:
        video = concatenate_videoclips(clips, method="compose")
        if video.duration < total_dur:
            n_loop = int(total_dur / video.duration) + 1
            video = concatenate_videoclips([video] * n_loop, method="compose")
        video = video.set_duration(total_dur)

    # ── 滤镜 ────────────────────────────────────────────────
    print("\n[3/4] 春日滤镜 + 合成...")
    # Warm spring color grading + subtle vignette
    def color_grade(frame):
        graded = np.clip(frame.astype(float) * [1.03, 1.0, 0.94] + [10, 4, -2], 0, 255)
        h, w = graded.shape[:2]
        # Subtle vignette: darken corners
        y, x = np.ogrid[:h, :w]
        cx, cy = w * 0.5, h * 0.45
        dist = np.sqrt(((x - cx) / (w * 0.75)) ** 2 + ((y - cy) / (h * 0.75)) ** 2)
        vignette = np.clip(1.0 - dist * 0.35, 0.7, 1.0)
        graded = np.clip(graded * vignette[:, :, np.newaxis], 0, 255)
        return graded.astype(np.uint8)

    video = video.fx(lambda c: c.fl_image(color_grade))

    # Title & date with glass style
    from datetime import datetime
    date_str = datetime.now().strftime("%Y年%m月%d日")

    title = make_text_clip("早安电台", fontsize=72, color="#FFFEF5",
                           stroke_color="#996677", stroke_width=3, max_width=int(W * 0.6), glass_bg=True)
    title = title.set_position(("center", int(H * 0.08)))
    title = title.set_duration(min(7, total_dur))
    title = title.crossfadein(1.0).crossfadeout(1.5)

    date_txt = make_text_clip(date_str, fontsize=34, color="#FFE8D6",
                              stroke_color="#775555", stroke_width=2, max_width=int(W * 0.45))
    date_txt = date_txt.set_position(("center", int(H * 0.20)))
    date_txt = date_txt.set_duration(min(7, total_dur))
    date_txt = date_txt.crossfadein(1.2).crossfadeout(1.5)

    subs = subtitle_clips(script_text, total_dur)

    # Compose
    final = CompositeVideoClip([video, title, date_txt] + subs, size=(W, H))
    final = final.set_duration(total_dur)
    final = final.set_audio(audio)

    # ── 导出 ────────────────────────────────────────────────
    output_path = OUTPUT_DIR / f"{output_name}.mp4"
    print(f"\n[4/4] 导出 → {output_path}")
    final.write_videofile(
        str(output_path),
        fps=24,
        codec="libx264",
        audio_codec="aac",
        bitrate="3000k",
        preset="medium",
        threads=4,
    )

    # Cleanup
    audio.close()
    final.close()
    for c in clips:
        try:
            c.close()
        except Exception:
            pass
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nOK: {output_path}  ({total_dur:.0f}s, {W}x{H})")
    return str(output_path)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--text", "-t", type=str, help="电台文案")
    p.add_argument("--file", "-f", type=str, help="文案文件")
    p.add_argument("--output", "-o", type=str, default="morning_radio")
    args = p.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        from datetime import datetime
        now = datetime.now()
        text = f"""各位听众朋友们，大家早上好！
今天是{now.year}年{now.month}月{now.day}日，星期{['一','二','三','四','五','六','日'][now.weekday()]}。
春风十里不如你，愿你今天心情如花般绽放。

今日热点：科技巨头发布新一代AI芯片，引发全球关注。
新能源汽车销量再创新高，绿色出行成为主流。
国际气候峰会达成新协议，各国承诺加大减排力度。

新的一天，新的开始。愿你今天工作顺利，心情愉快！
感谢收听早安电台，我们明天再见！"""

    build_video(text.strip(), args.output)
