<div align="center">

# 🖼️ 洋洋 · PixelCut 批量抠图工作台

**本地运行 · 完全免费 · 输出与原图同分辨率** 的批量 AI 抠图工具

拖入整个文件夹 → 一键批量抠图 → 透明背景 PNG 原分辨率输出
图片全程在本机处理，**不上传任何服务器**

[![许可证](https://img.shields.io/badge/许可证-PolyForm%20Noncommercial%201.0.0-orange)](LICENSE)
[![平台](https://img.shields.io/badge/平台-Windows-blue)]()
[![模型](https://img.shields.io/badge/内置模型-12%20款-brightgreen)]()
[![输出](https://img.shields.io/badge/输出-100%25%20原分辨率-blueviolet)]()
[![费用](https://img.shields.io/badge/费用-永久免费-success)]()
[![模型托管](https://img.shields.io/badge/模型-Hugging%20Face-FFD21E)](https://huggingface.co/jkyy/yang-pixelcut-models)

<img src="assets/screenshots/01-home.png" width="860" alt="主界面 · RMBG-3.0">

</div>

---

## 🆕 v1.4 · RMBG-3.0 全面落地

> BRIA 2026-11 发布的新一代 8.1B 抠图模型，官方在 234 张困难图上排名第 1，对 RMBG-2.0 胜率 **60.9%**（这是通用抠图质量的评测，不是提示词能力）。
> 它是按一条固定指令训练出来的：**官方明确不支持按提示词指定抠什么**，改指令还可能出彩色图。本工具的提示词框只算**实验性微调**，不保证稳定。

| 对比项 | RMBG-2.0 | **RMBG-3.0** |
|---|---|---|
| 定位 | 速度与批量 | **边缘质量：发丝、毛皮、玻璃、精细细节** |
| 按提示词抠指定物体 | **支持（官方能力）** | **不支持（官方口径），实验框只是把指令文本换一下** |
| 人坐椅子 | 椅子易丢失（前景 54%） | 单张自测：改指令后椅子保留变多（57.9%），样本量 1，不作指标 |
| 发丝 / 透明物 | 较好 | **明显更干净** |
| 本机速度（RTX 4060 Ti 8GB） | 约 10 秒/张 | 约 67~90 秒/张 |
| 模型体积 | 977 MB | 约 24 GB |

**顺手修的几个问题**

- 首次加载等待从 150 秒延长到 300 秒，避免还没加载完就降级出错误结果
- 服务启动后自动在后台预热模型，打开页面时已经在加载了
- 启动脚本改成 CRLF + GBK，Windows 控制台中文不再乱码
- `runtime_rmbg3` 已经自带 Python 和 PyTorch，拷到别的电脑直接能用

---

## ✨ 特性速览

| | |
|---|---|
| 🖱️ **批量处理** | 拖入图片或**整个文件夹**，确认后一键抠图；支持 JPG / PNG / WebP / BMP / TIFF |
| 🎯 **原分辨率输出** | 原图 5464×5464，抠出来仍是 5464×5464，**绝不缩水** |
| 🔒 **全程本地** | 模型与处理都在本机完成，图片不上传网络，无数量限制 |
| 🧠 **12 款模型** | **RMBG-3.0** / RMBG-2.0 / BiRefNet 四档 / U²-Net / ViTMatte / SAM 双档 / CLIPSeg |
| 💬 **提示词微调（实验）** | RMBG-3.0 的提示词框可以试，但官方不支持按提示词指定物体，结果时好时坏 |
| 🎨 **点选精修** | 结果不理想？在图上点几下补全人物、椅子等任何区域（SAM ViT-B / ViT-L 可选） |
| ✏️ **提示词补全** | 输入「椅子 / chair / 帽子…」自动补全物体，中英文均可，支持批量 |
| ⚡ **并发加速** | 1 / 2 / 3 档并发，多核更快 |
| 💾 **结果管理** | 自动分批次保存，支持下载全部 ZIP / 下载所选 / 打开结果夹 |

---

## 🚀 快速上手（3 步）

```text
① 拖入图片 / 整个文件夹   →   图片先进入列表，不会立即处理
② 确认无误，点「一键抠图」 →   批量处理，全程本机运行
③ 精修 / 下载              →   不满意的点选精修，最后下载全部 ZIP
```

> 「人坐椅」这类容易丢物体的图：用「完整增强 · RMBG+HRSOD」取并集，或抠完点「点选精修」把椅子补回来，比指望提示词稳。

---

## 📦 安装与运行

### 方式 A：绿色版（拷贝即用，推荐）

完整绿色版包含 `models`（约 30GB）与 `runtime` / `runtime_rmbg3`（约 5.5GB），受 GitHub 单文件 100MB 限制**不存放在本仓库**。拿到绿色版目录后，双击 `启动抠图工具.bat` 即可使用，**无需安装 Python**。

### 方式 B：从源码运行

```bash
pip install -r requirements.txt
python server.py
```

浏览器自动打开 `http://127.0.0.1:8532/`，首次选择模型时自动联网下载。

### 方式 C：Hugging Face 下载模型（推荐）

> 🌐 **模型地址**：[https://huggingface.co/jkyy/yang-pixelcut-models](https://huggingface.co/jkyy/yang-pixelcut-models)

```bash
pip install huggingface_hub
huggingface-cli download jkyy/yang-pixelcut-models --local-dir models
```

仓库已完整托管 **全部 12 款模型（约 30GB）**：
- 快速档 ONNX 模型在 `models/` 下（约 5.96GB）
- RMBG-3.0 完整权重在 `models/rmbg3/` 下（约 24GB，已转 diffusers 格式，可直接加载）

---

## 🧠 模型库

| 界面选项 | 模型 | 特点 |
|---|---|---|
| **★ RMBG-3.0 · 全球最强（新 · 可提示词）** | `bria-rmbg-3.0` | 2026-11 新一代 8.1B 模型，官方困难图排名第一，边缘细节最好；提示词框仅为实验性微调（官方不支持按提示词指定物体）；约 67~90 秒/张 |
| **全球最强 · RMBG-2.0** | `bria-rmbg` | 综合效果最好，人物/商品/杂物都稳，速度快（约 10 秒/张） |
| **完整增强 · RMBG+HRSOD** | `bria-rmbg + birefnet-hrsod` | 双模型取并集，最不易丢物体（人+椅连坐场景） |
| **高清 · BiRefNet-Lite** | `birefnet-general-lite` | 速度与效果平衡，适合大批量 |
| **人像专精 · BiRefNet-Portrait** | `birefnet-portrait` | 专注人物，头发/身体轮廓更贴 |
| **至尊 · BiRefNet-Massive** | `birefnet-massive` | 大数据训练，细节更强但慢 |
| **显著目标 · HRSOD** | `birefnet-hrsod` | 高分辨率，细节丰富 |
| **通用 · 更快** | `u2net` | 快速模型，低内存 |
| **人像 · U²-Net Human** | `u2net_human_seg` | 快速人像 |
| **精细边缘** | `vitmatte` | 发丝级 matting，修边缘白边/噪点 |
| **点选精修** | `sam`（ViT-B / ViT-L） | 交互式分割，点选补全区域 |
| **提示词补全** | `clipseg` | 按文字提示补全物体 |

### RMBG-3.0 运行要求

- NVIDIA 显卡、显存 **8GB 以上**（纯 CPU 可跑但很慢）
- **首次加载约 2~3 分钟**（24GB 权重映射；服务启动后自动后台预热）
- 未就绪时会**等待加载完成**（最长 5 分钟）而不是降级，保证结果不乱
- 8GB 显存下每张 67~90 秒属正常：24GB 模型必须分块进出显存，这是 PCIe 带宽决定的
- **开启 Windows「高性能」电源模式可再快约 2 倍**（CPU 侧编码提速明显）
- 许可：**CC-BY-NC-4.0（仅限个人非商业用途）**

---

## 📁 目录结构

```
yang-pixelcut/
├── 启动抠图工具.bat     ← 绿色版一键启动（CRLF + GBK，中文不乱码）
├── server.py             本地服务端（Flask）
├── rmbg3_server.py       RMBG-3.0 推理子服务（独立进程，不阻塞主服务）
├── rmbg3_infer.py        RMBG-3.0 命令行推理脚本
├── requirements.txt      依赖清单
├── web/
│   └── index.html        操作页面（单文件）
├── assets/screenshots/   README 截图
├── models/               模型目录（自动下载 / 绿色版内置）
│   └── rmbg3/            RMBG-3.0 权重（约 24GB）
├── runtime/              便携 Python 环境（ONNX 快速模型用）
├── runtime_rmbg3/        RMBG-3.0 推理环境（已完全自包含）
├── 抠图结果/             输出目录（按时间批次）
├── 使用说明.txt          完整图文使用说明
└── LICENSE               PolyForm Noncommercial 1.0.0
```

---

## ❓ 常见问题

**Q：为什么绿色版那么大（30GB+）？**
A：内置了 12 个开源抠图模型，其中 RMBG-3.0 单独占约 24GB。不需要的模型可以在 `models/` 下删除，不影响启动；删掉 `models/rmbg3` 后界面会自动回退到 RMBG-2.0。

**Q：RMBG-3.0 为什么这么慢？**
A：它是 8.1B 参数的大模型（24GB 权重），8GB 显存必须把权重分块搬进搬出。实测 RTX 4060 Ti 约 67~90 秒/张，显存 16GB 以上会明显更快。也试过 int8 量化、torch.compile，在这张卡上反而更慢（反量化开销大于搬运节省），所以最后保持逐层 offload。

**Q：人坐椅子，椅子总是被抠掉怎么办？**
A：优先用「完整增强 · RMBG+HRSOD」取并集，或者抠完点「点选精修」把椅子补回来。RMBG-3.0 的提示词框可以试，但它官方不支持按提示词指定物体，只是对指令文本敏感，结果时好时坏，别当稳定功能用。

**Q：第一次点抠图要等很久？**
A：首次使用需要加载 24GB 权重（约 2~3 分钟），服务启动时会自动预热。之后每张约 67~90 秒。启动时建议右键「以管理员身份运行」批处理，可开启高性能电源模式，快约 2 倍。

**Q：模型从哪里下载？**
A：绿色版内置；源码方式首次使用自动下载；也可以从 [Hugging Face](https://huggingface.co/jkyy/yang-pixelcut-models) 下载完整模型包（含 RMBG-3.0）。

**Q：打开就闪退 / 满屏"不是内部或外部命令"？**
A：已修复版本。请使用仓库中最新的 `启动抠图工具.bat`（CRLF + GBK 编码），并确认 `models`、`runtime`、`runtime_rmbg3` 都在项目根目录。

**Q：输出分辨率会缩水吗？**
A：不会。输出 PNG 与原图完全同分辨率，原图 5464×5464 抠出来仍是 5464×5464（已用 5464×5464 实图验证）。

**Q：需要联网/登录/付费吗？**
A：都不需要。全程本机处理，永久免费。

---

## 📖 使用帮助

<img src="assets/screenshots/04-help.png" width="860" alt="帮助中心 · RMBG-3.0 模型说明">

---

## ⚖️ 许可

本项目代码与界面采用 **PolyForm Noncommercial License 1.0.0**（可查看、可学习、可非商业使用，禁止商用）。

```text
Required Notice: Copyright (c) 2026 洋洋
```

各模型版权归其各自作者所有：

- RMBG-3.0 / RMBG-2.0 © BRIA AI（CC-BY-NC-4.0，仅非商业）
- BiRefNet 系列 © ZhengPeng7 等
- SAM © Meta AI
- 其余详见 [Hugging Face 模型卡片](https://huggingface.co/jkyy/yang-pixelcut-models)

> 个人学习/自用工具，请勿用于商业用途。有问题欢迎 [提交 Issue](https://github.com/yyds8170-ctrl/yang-pixelcut/issues)。
