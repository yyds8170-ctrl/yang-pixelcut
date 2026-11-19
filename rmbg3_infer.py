# -*- coding: utf-8 -*-
"""
RMBG-3.0 (Bria) standalone inference.
Fixed edit instruction -> 4-step distilled diffusion -> grayscale alpha matte -> original-resolution RGBA PNG.
Usage:
    python rmbg3_infer.py --image <src> --output <dst> [--device cpu|gpu]
"""
import argparse
import json
import os
import sys
import time

import torch
from PIL import Image

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'rmbg3')

INSTRUCTION = (
    "Generate a detailed grayscale alpha matte. Map the opaque foreground to white and the "
    "background to black. Produce soft, anti-aliased grayscale gradients at the edges of the "
    "subject to represent fine details and transparency."
)
PROMPT = json.dumps({"edit_instruction": INSTRUCTION})


def log(msg):
    print(f'[rmbg3] {msg}', flush=True)


def build_pipe(device='cpu'):
    from diffusers import BriaFibo2Pipeline, BriaFibo2Transformer2DModel

    dtype = torch.bfloat16
    t0 = time.time()
    log('loading transformer ...')
    transformer = BriaFibo2Transformer2DModel.from_pretrained(
        os.path.join(BASE, 'transformer'), dtype=dtype
    )
    log(f'transformer loaded {round(time.time()-t0,1)}s')
    log('loading pipeline (text_encoder / vae) ...')
    pipe = BriaFibo2Pipeline.from_pretrained(BASE, transformer=transformer, dtype=dtype)
    log(f'pipeline loaded {round(time.time()-t0,1)}s')

    if device == 'gpu':
        log('enabling sequential cpu offload (8GB VRAM mode)')
        pipe.enable_sequential_cpu_offload()
        gen_device = 'cuda'
    else:
        pipe.to('cpu')
        gen_device = 'cpu'
    try:
        pipe.set_progress_bar_config(disable=True)
    except Exception:
        pass
    return pipe, gen_device


def run(pipe, gen_device, src_path, out_path, instruction=None):
    src = Image.open(src_path).convert('RGB')
    prompt = json.dumps({"edit_instruction": instruction}) if instruction else PROMPT
    t0 = time.time()
    result = pipe(
        prompt=prompt,
        image=src,
        num_inference_steps=4,
        guidance_scale=1.0,
        generator=torch.Generator(gen_device).manual_seed(0),
    ).images[0]
    log(f'diffusion done {round(time.time()-t0,1)}s, raw size {result.size}')

    matte = result.convert('L').resize(src.size, Image.LANCZOS)  # white = keep
    out = src.copy()
    out.putalpha(matte)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out.save(out_path)
    log(f'saved {out_path} {out.size[0]}x{out.size[1]} total {round(time.time()-t0,1)}s')
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--device', default='cpu', choices=['cpu', 'gpu'])
    ap.add_argument('--prompt', default=None, help='自定义 edit_instruction')
    args = ap.parse_args()

    pipe, gen_device = build_pipe(args.device)
    run(pipe, gen_device, args.image, args.output, args.prompt)


if __name__ == '__main__':
    main()
