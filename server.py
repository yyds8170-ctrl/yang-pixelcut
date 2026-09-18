# -*- coding: utf-8 -*-
"""
批量抠图工具 - 本地服务端
- 完全本地运行，图片不上传到任何服务器
- 使用 rembg (U2-Net / BiRefNet 开源模型) 进行抠图
- 输出 PNG 与原图分辨率完全一致
"""
import base64
import io
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
import traceback
import webbrowser
import zipfile
from collections import OrderedDict
from pathlib import Path

from flask import Flask, abort, after_this_request, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageFilter, ImageOps
import numpy as np

ROOT = Path(__file__).resolve().parent
os.environ["U2NET_HOME"] = str(ROOT / "models")  # 模型缓存指向项目文件夹，离线可用
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import rembg  # noqa: E402  需在设置 U2NET_HOME 之后导入
import onnxruntime as ort  # noqa: E402

WEB_DIR = ROOT / "web"
OUT_ROOT = ROOT / "抠图结果"
OUT_ROOT.mkdir(exist_ok=True)

MODEL_KEYS = {
    "enhance": "bria-rmbg",              # 完整增强·RMBG+HRSOD 融合
    "rmbg2": "bria-rmbg",                # 全球最强·RMBG-2.0（默认）
    "hrsod": "birefnet-hrsod",           # 显著目标·高分辨率
    "quality": "birefnet-general-lite",  # 高清·快
    "portrait": "birefnet-portrait",     # 人像专精·BiRefNet
    "massive": "birefnet-massive",       # 至尊·大数据训练
    "fast": "u2net",                     # 通用·快速
    "human": "u2net_human_seg",          # 人像·U2Net
    "sam": "sam",                        # 点选精修·SAM-ViT-B（交互式，快速）
    "sam_l": "sam",                      # 点选精修·SAM-ViT-L（更精准，慢）
}
MODEL_MIN_BYTES = {  # 各模型文件实际大小（字节），不足 95% 视为未下载完成
    "enhance": 1_024_331_469,
    "rmbg2": 1_024_331_469,
    "hrsod": 972_666_916,
    "quality": 224_005_088,
    "portrait": 972_666_916,
    "massive": 972_666_916,
    "fast": 175_997_641,
    "human": 175_997_641,
    "sam": 359_000_000,
    "sam_l": 1_100_000_000,
}
MODEL_KEYS["clipseg"] = "clipseg"  # 提示词抠图·CLIPSeg（文字→掩膜）
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
# 批量抠图可选模型（不含交互式的 SAM/CLIPSeg，防止误入会话）
PROCESS_MODELS = {"rmbg2", "enhance", "hrsod", "quality", "portrait", "massive", "fast", "human"}
MAX_PIXELS = 100_000_000  # 约 1 亿像素，足够 5464×5464 并留余量

# 各模型的大致常驻内存（GB），用于大图处理前预检
_MODEL_MEM_GB = {
    "enhance": 3.2, "rmbg2": 1.6, "hrsod": 1.6, "quality": 0.7,
    "portrait": 1.6, "massive": 1.6, "fast": 0.4, "human": 0.4,
    "sam": 0.7, "sam_l": 1.9,
}
_broken_models = set()  # 已知文件损坏/无法加载的模型


def available_mem_gb():
    """查询 Windows 当前可用物理内存（GB），失败时返回 8（保守假设）。"""
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullAvailPhys / (1024.0 ** 3)
    except Exception:  # noqa: BLE001
        pass
    return 8.0

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 单文件上限 1GB

# ---------- 日志：同时输出到控制台与 server.log ----------
_LOG_F = ROOT / "server.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(_LOG_F, encoding="utf-8"), logging.StreamHandler()],
)
_log = logging.getLogger("matting")

_sessions = OrderedDict()   # 模型会话缓存（LRU）
_session_lock = threading.Lock()  # 会话创建锁（防并发重复加载泄漏）
_SESSIONS_MAX = 4           # 最多常驻会话数（超出按 LRU 释放，防内存暴涨）
_KEEP_SESSIONS = {"rmbg2"}  # 默认模型常驻，避免反复重载
# 推理并发信号量：默认 1（串行最稳），前端可选 2/3（多核提速，吃内存）
_infer_sem = threading.Semaphore(1)
_INFER_MAX = 1


def set_concurrency(n):
    """调整推理并发上限（1-3），仅在进程级生效。"""
    global _infer_sem, _INFER_MAX
    try:
        n = int(n)
    except (TypeError, ValueError):
        return
    n = max(1, min(3, n))
    if n != _INFER_MAX:
        _INFER_MAX = n
        _infer_sem = threading.Semaphore(n)


def get_model_dir():
    return ROOT / "models" / "models"


def session_dir(sid: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid or ""):
        abort(400, "非法会话ID")
    return OUT_ROOT / sid


def unique_path(directory: Path, stem: str) -> Path:
    p = directory / f"{stem}.png"
    i = 2
    while p.exists():
        p = directory / f"{stem}-{i}.png"
        i += 1
    return p


def thumb_b64(im: Image.Image, fmt: str) -> str:
    t = im.copy()
    t.thumbnail((360, 360))
    buf = io.BytesIO()
    if fmt == "JPEG":
        t.convert("RGB").save(buf, fmt, quality=85)
    else:
        t.save(buf, fmt)
    mime = "jpeg" if fmt == "JPEG" else "png"
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/api/health")
def health():
    models = {k: model_ready(k) and k not in _broken_models for k in MODEL_KEYS}
    return jsonify(ok=True, models=models, broken=sorted(_broken_models), version="1.2")


def model_file(key: str) -> Path:
    name = MODEL_KEYS[key]
    return get_model_dir() / name / f"{name}.onnx"


def model_ready(key: str) -> bool:
    if key == "enhance":  # 完整增强依赖 RMBG-2.0 与 HRSOD 两个模型
        return model_ready("rmbg2") and model_ready("hrsod")
    if key == "sam":  # SAM 精修：encoder + decoder 两个文件
        sam_dir = get_model_dir() / "sam"
        enc = sam_dir / "sam_vit_b_01ec64.encoder.onnx"
        dec = sam_dir / "sam_vit_b_01ec64.decoder.onnx"
        try:
            return enc.stat().st_size >= 350_000_000 and dec.stat().st_size >= 10_000_000
        except OSError:
            return False
    if key == "sam_l":  # SAM-ViT-L（更精准版）
        sam_dir = get_model_dir() / "sam"
        enc = sam_dir / "sam_vit_l_0b3195.encoder.onnx"
        dec = sam_dir / "sam_vit_l_0b3195.decoder.onnx"
        try:
            return enc.stat().st_size >= 1_100_000_000 and dec.stat().st_size >= 10_000_000
        except OSError:
            return False
    if key == "clipseg":  # 提示词抠图：onnx + tokenizer
        d = get_model_dir() / "clipseg"
        try:
            return (d / "model_quantized.onnx").stat().st_size >= 120_000_000 \
                and (d / "tokenizer.json").stat().st_size > 0
        except OSError:
            return False
    try:
        return model_file(key).stat().st_size >= MODEL_MIN_BYTES.get(key, 0) * 0.95
    except OSError:
        return False


def _evict_lru():
    """按 LRU 释放超限会话（串行模式才启用；并发模式让用户自行承担内存）。"""
    if _INFER_MAX > 1:
        return
    while len(_sessions) > _SESSIONS_MAX:
        victim = None
        for k in list(_sessions.keys()):
            if k not in _KEEP_SESSIONS:
                victim = k
                break
        if victim is None:
            break
        s0 = _sessions.pop(victim)
        try:
            del s0
        except Exception:  # noqa: BLE001
            pass
        _log.info("LRU 释放模型会话: %s", victim)


def get_session(key: str):
    """创建/复用模型会话；LRU 淘汰超限会话，防内存暴涨。"""
    s = _sessions.get(key)
    if s is None:
        with _session_lock:  # 防并发重复创建同一模型会话
            s = _sessions.get(key)
            if s is None:
                if not model_ready(key):
                    raise ValueError(f"模型 {MODEL_KEYS[key]} 尚未下载完成，请稍后重试或换其他模型")
                o = ort.SessionOptions()
                o.enable_cpu_mem_arena = False       # 不预占大内存池，用完即还
                o.enable_mem_pattern = False
                try:
                    o.intra_op_num_threads = min(8, os.cpu_count() or 8)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    if key == "sam_l":
                        s = rembg.new_session(
                            "sam", sam_model="sam_vit_l_0b3195", sess_opts=o
                        )
                    else:
                        s = rembg.new_session(MODEL_KEYS[key], sess_opts=o)
                except Exception:
                    _broken_models.add(key)
                    _log.exception("模型加载失败（可能损坏）: %s", key)
                    raise ValueError(
                        f"模型 {MODEL_KEYS[key]} 文件损坏或不完整，无法加载"
                        "（详细见 server.log；损坏模型可联系作者重新获取完整文件）"
                    )
                _broken_models.discard(key)
                _sessions[key] = s
                _log.info("加载模型会话: %s", key)
    _sessions.move_to_end(key)
    _evict_lru()
    return s


# ---------- 提示词抠图·CLIPSeg ----------
_clipseg_sess = None
_clipseg_tok = None
_CLIP_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_CLIP_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_CLIP_MAX = 77
_CLIP_SIZE = 352


def _clipseg_load():
    global _clipseg_sess, _clipseg_tok
    if _clipseg_sess is None:
        from tokenizers import Tokenizer
        d = get_model_dir() / "clipseg"
        _clipseg_tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        _clipseg_sess = ort.InferenceSession(
            str(d / "model_quantized.onnx"), providers=["CPUExecutionProvider"]
        )
    return _clipseg_sess, _clipseg_tok


def clipseg_mask(img, text):
    """CLIPSeg 文字提示 → 前景二值掩膜（原图尺寸 numpy bool）。
    用 Otsu 自适应阈值 + 最大连通域 + 膨胀，聚焦目标物体。"""
    sess, tok = _clipseg_load()
    enc = tok.encode(text)
    ids = enc.ids[:_CLIP_MAX] + [0] * (_CLIP_MAX - len(enc.ids))
    am = [1] * min(len(enc.ids), _CLIP_MAX) + [0] * (_CLIP_MAX - min(len(enc.ids), _CLIP_MAX))
    px = np.asarray(img.resize((_CLIP_SIZE, _CLIP_SIZE), Image.BILINEAR), dtype=np.float32) / 255.0
    px = ((px - _CLIP_MEAN) / _CLIP_STD).transpose(2, 0, 1)[None].astype(np.float32)
    logits = sess.run(
        None,
        {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([am], dtype=np.int64),
            "pixel_values": px,
        },
    )[0]
    # Otsu 自适应阈值
    v = logits.ravel()
    lo, hi = v.min(), v.max()
    if hi - lo < 1e-6:
        thr = lo
    else:
        hist, edges = np.histogram(v, bins=256, range=(float(lo), float(hi)))
        total = v.size
        sum_all = (edges[:-1] * hist).sum()
        sum_b = wb = 0.0
        best = -1.0
        thr = lo
        for i in range(256):
            wb += hist[i]
            if wb == 0:
                continue
            wf = total - wb
            if wf == 0:
                break
            sum_b += edges[i] * hist[i]
            mb = sum_b / wb
            mf = (sum_all - sum_b) / wf
            b = wb * wf * (mb - mf) ** 2
            if b > best:
                best = b
                thr = edges[i]
    # 收紧：取 Otsu 与 85 分位中较高的阈值，避免把大片背景并入
    thr = max(thr, float(np.percentile(logits, 85)))
    m = (logits > thr).astype(np.uint8)
    mup = np.asarray(
        Image.fromarray(m * 255).resize(img.size, Image.BILINEAR)
    )
    mup = np.asarray(
        Image.fromarray(mup).filter(ImageFilter.MaxFilter(5))
    ) > 127
    # 取最大连通域（目标物体）——用 scipy 向量化，避免 Python 双层循环拖慢大图
    if not bool(mup.any()):
        return np.zeros((img.height, img.width), dtype=bool)
    lbl, n = ndimage.label(mup)
    if n == 0:
        return np.zeros((img.height, img.width), dtype=bool)
    sizes = ndimage.sum(mup.astype(np.uint8), lbl, index=range(1, n + 1))
    best_lbl = int(np.argmax(sizes) + 1)
    return lbl == best_lbl


# ---------- 快速边缘去污染（标准模式） ----------
# 原理：半透明边缘像素颜色 = 前景×alpha + 背景×(1-alpha)，若不修正，
# 在深背景/棋盘格上显示会"发黑/发灰"（如浅色 T 恤被扣成半透明后显黑）。
# 这里只处理边缘环带：用最近"实心前景"颜色按污染程度混合修正 + 轻度提实 alpha。
from scipy import ndimage  # noqa: E402


def fast_edge_fix(out, mix=0.55, boost=0.8):
    """快速边缘去色污染：仅处理 alpha 过渡带，控制在大约 ≤2048 分辨率计算以省内存。"""
    a0 = np.asarray(out.getchannel("A"), dtype=np.float32) / 255.0
    rgb0 = np.asarray(out.convert("RGB"), dtype=np.float32)
    scale = min(1.0, 2048 / float(max(a0.shape)))
    if scale < 1.0:
        small = out.resize(
            (max(1, int(out.width * scale)), max(1, int(out.height * scale))), Image.BILINEAR
        )
        a = np.asarray(small.getchannel("A"), dtype=np.float32) / 255.0
        rgb = np.asarray(small.convert("RGB"), dtype=np.float32)
    else:
        a, rgb = a0, rgb0
    solid = a >= 0.98
    if solid.sum() < 16 or (solid.sum() / float(solid.size)) > 0.999:
        return out  # 无实心前景或几乎全前景，无需处理
    try:
        dist, idx = ndimage.distance_transform_edt(~solid, return_indices=True)
        nearest = rgb[idx[0], idx[1]]
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return out
    edge = (a > 0.001) & (a < 0.98)
    w = np.clip(1.0 - a, 0.0, 1.0) ** 0.5 * mix
    rgb2 = rgb.copy()
    rgb2[edge] = rgb[edge] * (1.0 - w[edge][..., None]) + nearest[edge] * w[edge][..., None]
    a2 = a.copy()
    boost_zone = edge & (dist <= 5)
    a2[boost_zone] = np.clip(a[boost_zone] ** boost, 0.0, 1.0)
    small_out = Image.fromarray(
        np.concatenate([rgb2, (a2 * 255).astype(np.uint8)[..., None]], axis=-1).astype(np.uint8),
        "RGBA",
    )
    if scale < 1.0:
        small_out = small_out.resize(out.size, Image.LANCZOS)
    return small_out


def soften_mask(m, erode=2, blur=1.5):
    """二值掩膜 → 软边 alpha（主体实心 255，边缘渐变），避免提示词补全的硬边。"""
    inner = ndimage.binary_erosion(m, iterations=erode)
    m8 = (m.astype(np.uint8)) * 255
    soft = np.asarray(Image.fromarray(m8).filter(ImageFilter.GaussianBlur(blur)))
    soft = np.clip(soft, 0, 255)
    soft[inner] = 255
    return soft


# 常用中文提示词 → 英文（CLIPSeg 用英文词表）
_CN2EN = {
    "人": "person", "人物": "person", "人体": "person", "人像": "person", "全身": "person",
    "椅子": "chair", "凳子": "chair", "座椅": "chair", "沙发": "sofa", "桌子": "table", "茶几": "table",
    "帽子": "hat", "帽子": "hat", "背包": "backpack", "鞋": "shoes", "鞋子": "shoes",
    "头发": "hair", "长发": "hair", "毛衣": "sweater", "衣服": "clothes", "上衣": "shirt",
    "裤子": "pants", "牛仔裤": "jeans", "裙子": "dress", "连衣裙": "dress", "外套": "jacket",
    "夹克": "jacket", "卫衣": "hoodie", "眼镜": "glasses", "口罩": "mask", "围巾": "scarf",
    "领带": "tie", "手套": "gloves", "花": "flower", "花束": "bouquet", "树": "tree",
    "植物": "plant", "盆栽": "plant", "汽车": "car", "自行车": "bike", "摩托车": "motorcycle",
    "狗": "dog", "猫": "cat", "宠物": "pet", "动物": "animal", "鸟": "bird",
    "手机": "phone", "电脑": "laptop", "笔记本": "laptop", "相机": "camera", "耳机": "headphones",
    "杯子": "cup", "水杯": "cup", "瓶子": "bottle", "水瓶": "bottle", "书本": "book", "书": "book",
    "行李箱": "luggage", "拉杆箱": "luggage", "灯": "lamp", "台灯": "lamp",
}


def translate_prompt(p):
    """中文提示词 → 英文（支持复合描述，把已知中文词全部替换为英文）。
    例如「戴帽子的女人」→「戴hat的女person」，并自动提取首个物体词「hat」，
    避免残留中文（戴/的/坐着等动词介词）被误判未识别。"""
    p = (p or "").strip()
    if not p:
        return p
    pl = p.lower()
    if pl in _CN2EN:
        return _CN2EN[pl]
    for cn, en in _CN2EN.items():
        p = p.replace(cn, en)
    # 仍残留中文：提取已翻译出的英文物体词（用第一个），让复合描述也能用
    if re.search(r"[\u4e00-\u9fff]", p):
        words = re.findall(r"[A-Za-z]+", p)
        if words:
            return words[0]
    return p


def union_rgba(a, b):
    """两个 RGBA 取 alpha 并集，颜色按 alpha 来源加权合成。
    避免"只用一方 alpha、另一方背景 RGB=0 的黑边/黑剪影"。"""
    a1 = np.asarray(a.getchannel("A"), dtype=np.float32) / 255.0
    a2 = np.asarray(b.getchannel("A"), dtype=np.float32) / 255.0
    au = np.maximum(a1, a2)
    w1 = a1 / np.maximum(au, 1e-6)
    rgb1 = np.asarray(a.convert("RGB"), dtype=np.float32)
    rgb2 = np.asarray(b.convert("RGB"), dtype=np.float32)
    rgb = rgb1 * w1[..., None] + rgb2 * (1.0 - w1)[..., None]
    return Image.fromarray(
        np.concatenate(
            [rgb.astype(np.uint8), (au * 255).astype(np.uint8)[..., None]], axis=-1
        ),
        "RGBA",
    )


def _infer_enhance(img):
    """完整增强：RMBG-2.0 与 HRSOD 的掩膜取并集，再做边缘保护膨胀，
    最大化保留前景主体（如人物+椅子等相连物体）。"""
    out_r = rembg.remove(img, session=get_session("rmbg2"))  # RGBA
    out_h = rembg.remove(img, session=get_session("hrsod"))
    a_r = np.asarray(out_r.getchannel("A"), dtype=np.float32) / 255.0
    a_h = np.asarray(out_h.getchannel("A"), dtype=np.float32) / 255.0
    # 颜色按 alpha 来源合成（RMBG/HRSOD 各自的前景色）
    out = union_rgba(out_r, out_h)
    mask = (np.asarray(out.getchannel("A"), dtype=np.float32) / 255.0 > 0.45).astype(np.uint8) * 255
    out.putalpha(Image.fromarray(mask, "L").filter(ImageFilter.MaxFilter(7)))
    return out


def infer_with_fallback(img, key: str):
    """执行抠图；大模型不可用时自动降级到快速模型，返回 (结果, 实际模型key, 原因)。"""
    if key == "enhance":
        try:
            return _infer_enhance(img), key, None
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "allocat" in msg or "memory" in msg:
                traceback.print_exc()
                s = get_session("fast")
                return rembg.remove(img, session=s), "fast", "memory"
            raise
    try:
        s = get_session(key)
        return rembg.remove(img, session=s), key, None
    except ValueError:
        if key != "fast":  # 模型文件未就绪
            traceback.print_exc()
            s = get_session("fast")
            return rembg.remove(img, session=s), "fast", "notready"
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if key != "fast" and ("allocat" in msg or "memory" in msg or "fail" in msg):
            traceback.print_exc()
            s = get_session("fast")
            return rembg.remove(img, session=s), "fast", "memory"
        raise


@app.post("/api/refine")
def api_refine():
    """点选精修：用户在图上点选要保留的区域（SAM 分割），与当前抠图结果取并集补全。"""
    f = request.files.get("file")
    if f is None:
        return jsonify(ok=False, error="没有收到文件"), 400
    sid = request.form.get("session", "default")
    sam_model = request.form.get("sam_model", "vit_b")
    if sam_model not in ("vit_b", "vit_l"):
        sam_model = "vit_b"
    try:
        points = json.loads(request.form.get("points", "[]"))
    except Exception:
        points = []
    if not isinstance(points, list) or not points or not all(
        isinstance(p, list) and len(p) >= 2 and len(p) <= 3 for p in points
    ):
        return jsonify(ok=False, error="请先在图上点选要保留/去除的区域"), 400
    base_url = request.form.get("base", "")
    raw = f.read()
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception:
        return jsonify(ok=False, error="无法识别的图片格式"), 400
    if img.mode != "RGB":
        img = img.convert("RGB")
    t0 = time.time()
    try:
        with _infer_sem:
            skey = "sam_l" if sam_model == "vit_l" else "sam"
            if not model_ready(skey):
                return jsonify(ok=False, error="所选 SAM 模型尚未下载完成，请稍后重试或换用 ViT-B"), 400
            sess = get_session(skey)
            sam_prompt = [
                {
                    "type": "point",
                    "label": int(p[2]) if len(p) > 2 else 1,  # 1=保留 0=去除
                    "data": [float(p[0]), float(p[1])],
                }
                for p in points
            ]
            out = rembg.remove(img, session=sess, sam_prompt=sam_prompt)
            # 与当前抠图结果取并集，补全缺失部分
            if base_url:
                try:
                    path_part = base_url.split("/result/", 1)[-1]
                    if ".." in path_part or path_part.startswith("/") or not path_part:
                        base_path = None
                    else:
                        base_path = OUT_ROOT / path_part
                    if base_path and base_path.exists():
                        base_img = Image.open(base_path).convert("RGBA")
                        # 颜色按来源合成，SAM 补的区域用原图色、原结果区域用原结果色
                        out = union_rgba(out, base_img)
                except Exception:
                    traceback.print_exc()
                    _log.exception("refine 并集失败")
    except Exception:
        traceback.print_exc()
        return jsonify(ok=False, error="精修处理失败，请重试"), 500
    ms = int((time.time() - t0) * 1000)
    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    stem = Path(f.filename or "精修").stem
    out_name = f"{stem}-refined.png"
    p = d / out_name
    out.save(p, "PNG")
    return jsonify(
        ok=True,
        url=f"/result/{sid}/{out_name}",
        outName=out_name,
        w=out.width,
        h=out.height,
        ms=ms,
    )


@app.post("/api/segment")
def api_segment():
    """提示词抠图：CLIPSeg 根据文字提示定位物体（如 chair/hat/bag），
    与当前抠图结果取并集补全缺失的物体区域。"""
    f = request.files.get("file")
    if f is None:
        return jsonify(ok=False, error="没有收到文件"), 400
    prompt_orig = (request.form.get("prompt") or "").strip()
    if not prompt_orig:
        return jsonify(ok=False, error="请输入要补全的物体提示词（如 chair / 椅子）"), 400
    prompt = translate_prompt(prompt_orig)
    if re.search(r"[\u4e00-\u9fff]", prompt):
        return jsonify(
            ok=False,
            error=f"未识别「{prompt_orig}」，可试试：椅子/帽子/包/人/头发/车/植物/手机，或英文 chair/hat/bag/person/hair/car/plant/phone",
        ), 400
    sid = request.form.get("session", "default")
    base_url = request.form.get("base", "")
    raw = f.read()
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception:
        return jsonify(ok=False, error="无法识别的图片格式"), 400
    if img.mode != "RGB":
        img = img.convert("RGB")
    t0 = time.time()
    try:
        with _infer_sem:
            if not model_ready("clipseg"):
                return jsonify(ok=False, error="提示词抠图模型尚未下载完成，请稍后重试"), 400
            m = clipseg_mask(img, prompt)
            fg_ratio = float(m.mean()) * 100
            if fg_ratio < 2.0:
                return jsonify(ok=False, error=f"未识别到「{prompt}」，请换个说法（建议英文如 chair / hat / bag / person）"), 400
            a_new = soften_mask(m)  # 软边 alpha（主体实心、边缘渐变）
            # 关键：前景必须保留原图彩色（不能是纯黑 RGB）
            out = Image.new("RGBA", img.size, (0, 0, 0, 0))
            out.paste(img, (0, 0), Image.fromarray(a_new, "L"))
            # 与当前抠图结果并集补全
            if base_url:
                try:
                    path_part = base_url.split("/result/", 1)[-1]
                    if ".." in path_part or path_part.startswith("/") or not path_part:
                        base_path = None
                    else:
                        base_path = OUT_ROOT / path_part
                    if base_path and base_path.exists():
                        base_img = Image.open(base_path).convert("RGBA")
                        a_base = np.asarray(base_img.getchannel("A"), dtype=np.float32) / 255.0
                        a_seg = np.asarray(out.getchannel("A"), dtype=np.float32) / 255.0
                        a_union = np.maximum(a_base, a_seg)
                        # 颜色合成：alpha 主要来自原结果处用原结果颜色，新增区域用原图颜色
                        w_base = a_base / np.maximum(a_union, 1e-6)
                        base_rgb = np.asarray(base_img.convert("RGB"), dtype=np.float32)
                        img_rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
                        rgb = base_rgb * w_base[..., None] + img_rgb * (1.0 - w_base)[..., None]
                        out = Image.fromarray(
                            np.concatenate(
                                [rgb.astype(np.uint8), (a_union * 255).astype(np.uint8)[..., None]],
                                axis=-1,
                            ),
                            "RGBA",
                        )
                except Exception:
                    traceback.print_exc()
    except Exception:
        traceback.print_exc()
        return jsonify(ok=False, error="提示词抠图处理失败，请重试"), 500
    ms = int((time.time() - t0) * 1000)
    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    stem = Path(f.filename or "提示词抠图").stem
    out_name = f"{stem}-segment.png"
    p = d / out_name
    out.save(p, "PNG")
    return jsonify(
        ok=True,
        url=f"/result/{sid}/{out_name}",
        outName=out_name,
        w=out.width,
        h=out.height,
        ms=ms,
        fgRatio=round(fg_ratio, 1),
    )


@app.post("/api/process")
def api_process():
    f = request.files.get("file")
    if f is None:
        return jsonify(ok=False, error="没有收到文件"), 400
    sid = request.form.get("session", "default")
    key = request.form.get("model", "rmbg2")
    if key not in PROCESS_MODELS:
        key = "quality"
    matting = request.form.get("matting") == "1"
    set_concurrency(request.form.get("concurrency", "1"))
    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)

    raw = f.read()
    name = Path(f.filename or "图片").name or "图片"
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception:
        return jsonify(ok=False, error="无法识别的图片格式"), 400
    if img.width * img.height > MAX_PIXELS:
        return jsonify(ok=False, error=f"图片过大（{img.width}×{img.height}），超出处理上限"), 400
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 内存预检：估算本图处理所需内存，明显不足时提前拦截，避免跑到一半卡死/降级
    try:
        est_gb = img.width * img.height * 4 * 4 / 1.0e9 + _MODEL_MEM_GB.get(key, 1.0)
        avail_gb = available_mem_gb()
        if est_gb > avail_gb * 0.75:
            return jsonify(
                ok=False,
                error=f"图片 {img.width}×{img.height} 较大，本图预计需约 {est_gb:.1f}GB 内存"
                f"（当前可用 {avail_gb:.1f}GB）。建议：① 换「高清/通用·更快」轻量模型 ② 关闭"
                "Photoshop 等大程序后重试",
            ), 413
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    t0 = time.time()
    warning = None
    try:
        with _infer_sem:
            out, used_key, reason = infer_with_fallback(img, key)
            if used_key != key:
                if reason == "notready":
                    if key in _broken_models:
                        warning = "所选模型文件损坏，本张已临时改用「通用·快速」模型（可查看 server.log）"
                    else:
                        warning = "所选模型文件尚未下载完成，本张已临时改用「通用·快速」模型，稍后可重试"
                else:
                    warning = "系统虚拟内存不足，本张已自动改用「通用·快速」模型；关闭 Photoshop 等大程序后可继续用高清模型"
            if matting:
                # 精细边缘升级：ViTMatte 网络（比 pymatting 闭式求解更快、发丝细节更好），
                # 阈值更保发丝；输出自动去色污染，避免边缘残留背景色/发黑
                try:
                    out = rembg.remove(
                        img,
                        session=get_session(used_key),
                        vitmatte=True,
                        alpha_matting_foreground_threshold=200,
                        alpha_matting_background_threshold=20,
                        alpha_matting_erode_size=8,
                    )
                except Exception:
                    traceback.print_exc()
                    warning = (warning + "；" if warning else "") + "精细边缘不可用，已按标准模式完成"
            else:
                # 标准模式：快速边缘去色污染（修 T恤发黑/发丝泛色，仅边缘带，秒级）
                try:
                    out = fast_edge_fix(out)
                except Exception:
                    traceback.print_exc()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(ok=False, error=f"处理失败：{e}"), 500

    if out.size != img.size:  # 保险：确保输出与原图同尺寸
        out = out.resize(img.size, Image.LANCZOS)

    stem = Path(name).stem or "image"
    out_path = unique_path(d, stem)
    out.save(out_path, "PNG")
    ms = int((time.time() - t0) * 1000)

    return jsonify(
        ok=True,
        name=name,
        outName=out_path.name,
        w=img.width,
        h=img.height,
        ms=ms,
        warning=warning,
        before=thumb_b64(img, "JPEG"),
        after=thumb_b64(out, "PNG"),
        url=f"/result/{sid}/{out_path.name}",
    )


@app.get("/result/<sid>/<path:fname>")
def result_file(sid, fname):
    return send_from_directory(session_dir(sid), fname, conditional=True)


@app.get("/api/zip/<sid>")
def zip_session(sid):
    d = session_dir(sid)
    names = request.args.get("files", "")
    if names:  # 下载所选：逗号分隔文件名（只允许纯文件名，防路径穿越）
        files = []
        for n in names.split(","):
            n = n.strip()
            if not n or "/" in n or "\\" in n or n in (".", ".."):
                continue
            f = d / n
            if f.exists() and f.is_file() and f.suffix.lower() == ".png":
                files.append(f)
    else:
        files = sorted(d.glob("*.png"))
    if not files:
        abort(404, "没有可下载的结果")
    zip_path = OUT_ROOT / f"{sid}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # PNG 已压缩，直接存储最快
        for p in files:
            z.write(p, arcname=p.name)

    @after_this_request
    def _cleanup(resp):
        try:
            zip_path.unlink()
        except OSError:
            pass
        return resp

    return send_file(zip_path, as_attachment=True, download_name=f"抠图结果_{sid}.zip")


@app.post("/api/open/<sid>")
def open_folder(sid):
    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    os.startfile(str(d))  # noqa: S606  仅 Windows 本地资源管理器
    return jsonify(ok=True)


def pick_port():
    for p in range(8532, 8543):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 8532


if __name__ == "__main__":
    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    print("=" * 46, flush=True)
    print("  批量抠图工具已启动", flush=True)
    print(f"  地址: {url}", flush=True)
    print("  浏览器将自动打开；关闭本窗口即退出服务", flush=True)
    print("  结果保存在: " + str(OUT_ROOT), flush=True)
    print("=" * 46, flush=True)
    if not os.environ.get("RMT_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
