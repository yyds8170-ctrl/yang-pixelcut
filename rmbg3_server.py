# -*- coding: utf-8 -*-
"""
RMBG-3.0 worker service (Bria Fibo-2.0-edit-turbo distilled, 8.1B).
Keeps the diffusion model resident and exposes:
    GET  /health                  -> {ok, ready, loading, error, device}
    POST /infer                   -> multipart (file, prompt?)
    POST /infer_json              -> JSON {image_b64, prompt?} => {ok, alpha, w, h, ms}
Run with the torch-capable interpreter (runtime_rmbg3).
"""
import base64
import io
import json
import os
import threading
import time
import traceback

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, 'models', 'rmbg3')

DEFAULT_INSTRUCTION = (
    "Generate a detailed grayscale alpha matte. Map the opaque foreground to white and the "
    "background to black. Produce soft, anti-aliased grayscale gradients at the edges of the "
    "subject to represent fine details and transparency."
)

app = Flask(__name__)
_pipe = None
_gen_device = 'cpu'
_device_mode = None
_infer_lock = threading.Lock()
_state = {'ready': False, 'loading': False, 'error': None, 't0': None, 'device': None}

# 常用中文主体 → 英文（RMBG-3.0 的指令以英文训练，中文先做术语映射）
_CN2EN = [
    ('人物', 'the person'), ('人像', 'the person'), ('模特', 'the person'),
    ('模特儿', 'the person'), ('人', 'the person'),
    ('椅子', 'the chair'), ('凳', 'the chair'), ('沙发', 'the sofa'),
    ('桌子', 'the table'), ('桌', 'the table'),
    ('衣服', 'the clothing'), ('服装', 'the clothing'), ('上衣', 'the top'),
    ('裤子', 'the pants'), ('裙', 'the skirt'), ('外套', 'the jacket'),
    ('鞋', 'the shoes'), ('靴', 'the boots'), ('包', 'the bag'), ('帽', 'the hat'),
    ('头发', 'the hair'), ('手', 'the hands'), ('脸', 'the face'),
    ('猫', 'the cat'), ('狗', 'the dog'), ('宠物', 'the pet'),
    ('产品', 'the product'), ('商品', 'the product'),
    ('车', 'the car'), ('植物', 'the plant'), ('花', 'the flowers'),
    ('和', ' and '), ('与', ' and '), ('跟', ' and '),
    ('，', ', '), ('、', ', '), ('+', ' and '),
    ('只', 'only '), ('全部', 'everything'), ('所有', 'everything'),
    ('不要', 'except '), ('除了', 'except '),
]


def _localize(prompt: str) -> str:
    """中文提示词 → 英文主体描述（已经是英文则原样使用）。"""
    if not prompt:
        return ''
    has_cn = any('\u4e00' <= c <= '\u9fff' for c in prompt)
    if not has_cn:
        return prompt.strip()
    out = prompt
    for cn, en in _CN2EN:
        out = out.replace(cn, en)
    out = ''.join(c for c in out if not ('\u4e00' <= c <= '\u9fff'))
    out = ' '.join(out.replace(',,', ',').split())
    return out.strip(' ,')


def build_prompt(prompt: str) -> str:
    """把用户提示词组装成 RMBG-3.0 的 edit_instruction。"""
    subject = _localize(prompt)
    if not subject:
        return DEFAULT_INSTRUCTION
    return (
        "Generate a detailed grayscale alpha matte: white marks the foreground, black marks the "
        f"background. The foreground subject is {subject}. Mark that subject fully white and fully "
        "opaque, including all of its visible parts even where they are far from each other. "
        "Everything else stays black. Keep soft, anti-aliased grayscale gradients at the subject's "
        "edges to preserve fine details such as hair and transparency."
    )


def log(m):
    print(f'[rmbg3-worker] {m}', flush=True)


def build_pipe(device='cpu'):
    global _pipe, _gen_device, _device_mode
    from diffusers import BriaFibo2Pipeline, BriaFibo2Transformer2DModel
    import torch

    dtype = torch.bfloat16
    log('loading transformer ...')
    transformer = BriaFibo2Transformer2DModel.from_pretrained(
        os.path.join(MODEL_DIR, 'transformer'), dtype=dtype
    )
    log('loading pipeline ...')
    pipe = BriaFibo2Pipeline.from_pretrained(
        MODEL_DIR, transformer=transformer, dtype=dtype
    )
    if device == 'gpu':
        mode = os.environ.get('RMBG3_OFFLOAD', 'sequential')
        import torch

        # 编码端视觉分辨率：768 最准，512 可把 VLM 阶段耗时约减半
        try:
            tes = int(os.environ.get('RMBG3_TEXT_IMAGE_SIZE', '768') or 768)
            if tes > 0:
                pipe.text_encoder_image_size = tes
                log(f'text_encoder_image_size = {tes}')
        except Exception:  # noqa: BLE001
            pass

        if mode == 'fp8':
            try:
                from diffusers.hooks import apply_layerwise_casting, apply_group_offloading
                apply_layerwise_casting(
                    pipe.text_encoder,
                    storage_dtype=torch.float8_e4m3fn,
                    compute_dtype=torch.bfloat16,
                    non_blocking=True,
                )
                pipe.text_encoder.to('cuda')
                pipe.vae.to('cuda')
                apply_group_offloading(
                    pipe.transformer,
                    onload_device=torch.device('cuda'),
                    offload_device=torch.device('cpu'),
                    offload_type='leaf_level',
                    use_stream=True,
                    record_stream=True,
                )
                log('text_encoder fp8 常驻 GPU + transformer leaf-level offload')
            except Exception as e:  # noqa: BLE001
                log(f'fp8 常驻失败，回退整管线 offload：{e}')
                pipe.enable_sequential_cpu_offload()
        elif mode == 'group':
            log('block-level offload：transformer 1 块/组 + text_encoder 2 块/组（streamed）')
            try:
                from diffusers.hooks import apply_group_offloading
                apply_group_offloading(
                    pipe.transformer,
                    onload_device=torch.device('cuda'),
                    offload_device=torch.device('cpu'),
                    offload_type='block_level',
                    num_blocks_per_group=1,
                    use_stream=True,
                    record_stream=True,
                )
                apply_group_offloading(
                    pipe.text_encoder,
                    onload_device=torch.device('cuda'),
                    offload_device=torch.device('cpu'),
                    offload_type='block_level',
                    num_blocks_per_group=2,
                    use_stream=True,
                )
                pipe.vae.to('cuda')
            except Exception as e:  # noqa: BLE001
                log(f'group offload 失败，回退 sequential：{e}')
                pipe.enable_sequential_cpu_offload()
        else:
            log('enable_sequential_cpu_offload (low VRAM mode)')
            pipe.enable_sequential_cpu_offload()
        _gen_device = 'cuda'
    else:
        pipe.to('cpu')
        _gen_device = 'cpu'
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass
    _pipe = pipe
    _device_mode = device
    _state['ready'] = True
    _state['device'] = device
    log(f'pipeline ready on {device} ({round(time.time()-_state["t0"],1)}s)')


@app.get('/health')
def health():
    return jsonify(
        ok=True,
        ready=_state['ready'],
        loading=_state['loading'],
        error=_state['error'],
        device=_state['device'],
    )


@app.post('/load')
def load():
    if _state['ready'] or _state['loading']:
        return jsonify(ok=True, ready=_state['ready'], loading=_state['loading'])
    device = request.values.get('device', os.environ.get('RMBG3_DEVICE', 'cpu'))
    _state['loading'] = True
    _state['t0'] = time.time()

    def _run():
        try:
            build_pipe(device)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _state['error'] = str(e)
        finally:
            _state['loading'] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, loading=True)


def _cutout(img, prompt, size=1024):
    from PIL import Image
    import torch

    payload = json.dumps({'edit_instruction': build_prompt(prompt)})
    w, h = img.size
    if size and size > 0 and min(w, h) > 0:
        scale = size / min(w, h)
        tw, th = max(64, round(w * scale / 16) * 16), max(64, round(h * scale / 16) * 16)
    else:
        tw = th = None
    t0 = time.time()
    kw = {}
    if tw and th:
        kw['width'] = tw
        kw['height'] = th
    marks = {'start': t0}

    def _cb(pipe, step, timestep, callback_kwargs):
        marks.setdefault('steps', []).append(time.time())
        return callback_kwargs

    with _infer_lock:
        result = _pipe(
            prompt=payload,
            image=img,
            num_inference_steps=4,
            guidance_scale=1.0,
            generator=torch.Generator(_gen_device).manual_seed(0),
            callback_on_step_end=_cb,
            **kw,
        ).images[0]
    t_end = time.time()
    matte = result.convert('L').resize(img.size, Image.LANCZOS)
    ms = int((t_end - t0) * 1000)
    steps = marks.get('steps', [])
    if steps:
        pre = steps[0] - t0
        seg = [steps[i + 1] - steps[i] for i in range(len(steps) - 1)]
        log(f'timing: pre(text_encode)={pre * 1000:.0f}ms  steps={["%.0f" % (s * 1000) for s in seg]}  post={(t_end - steps[-1]) * 1000:.0f}ms  total={ms}ms  render={tw}x{th}')
    return matte, ms, tw, th


def _decode_image(raw):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img.load()
    if img.mode != 'RGB':
        img = img.convert('RGB')
    return img


def _alpha_b64(matte):
    buf = io.BytesIO()
    matte.save(buf, 'PNG')
    return base64.b64encode(buf.getvalue()).decode()


@app.post('/infer')
def infer():
    if not _state['ready']:
        return jsonify(ok=False, error=_state['error'] or 'RMBG-3.0 模型尚未加载完成，请稍候'), 503
    f = request.files.get('file')
    if f is None:
        return jsonify(ok=False, error='没有收到文件'), 400
    prompt = request.form.get('prompt', '') or None
    try:
        size = int(request.form.get('size', '1024') or 1024)
    except ValueError:
        size = 1024
    try:
        img = _decode_image(f.read())
        matte, ms, tw, th = _cutout(img, prompt, size)
        return jsonify(ok=True, alpha=_alpha_b64(matte), w=img.width, h=img.height,
                       ms=ms, render_w=tw, render_h=th)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(ok=False, error=f'推理失败：{e}'), 500


@app.post('/infer_json')
def infer_json():
    if not _state['ready']:
        return jsonify(ok=False, error=_state['error'] or 'RMBG-3.0 模型尚未加载完成，请稍候'), 503
    data = request.get_json(silent=True) or {}
    b64 = data.get('image_b64') or ''
    prompt = (data.get('prompt') or '').strip() or None
    try:
        size = int(data.get('size', 1024) or 1024)
    except (TypeError, ValueError):
        size = 1024
    if not b64:
        return jsonify(ok=False, error='缺少 image_b64'), 400
    try:
        img = _decode_image(base64.b64decode(b64))
        matte, ms, tw, th = _cutout(img, prompt, size)
        return jsonify(ok=True, alpha=_alpha_b64(matte), w=img.width, h=img.height,
                       ms=ms, render_w=tw, render_h=th)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(ok=False, error=f'推理失败：{e}'), 500


def pick_port(default=8543):
    import socket
    for p in range(default, default + 10):
        s = socket.socket()
        try:
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except OSError:
            s.close()
    return default


if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else pick_port()
    log(f'listening on 127.0.0.1:{port}')
    _state['loading'] = True
    _state['t0'] = time.time()
    threading.Thread(
        target=lambda: build_pipe(os.environ.get('RMBG3_DEVICE', 'cpu')), daemon=True
    ).start()
    app.run(host='127.0.0.1', port=port, threaded=True)
