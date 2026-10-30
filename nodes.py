# -*- coding: utf-8 -*-
"""
H3PromptGenerator — ComfyUI 自定义节点 (方案2: 纯提示词工程, 不调用 LLM)

选择模式 (T2VA / I2VA / FL2VA / L2VA / Ref2VA) → 加载该模式专项 system prompt
→ 注入 视频时长 / 视觉风格 / 音乐风格 参数块 → 输出 system_prompt + user_prompt。

接线 (ComfyUI-llama-cpp_vlm 插件):
  system_prompt → llama_cpp_instruct_adv.system_prompt
  user_prompt   → llama_cpp_instruct_adv.custom_prompt
  preset_prompt 设为 "Empty - Nothing"
  模型加载/显存管理由 llama_cpp_model_loader + llama_cpp_parameters 负责
  (vlm 插件自带 force_offload / unload_all_models hook), 本节点不做任何 LLM 调用。

零第三方依赖, 不 import comfy 模块, 可独立测试。
"""
import asyncio
import collections
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# ─────────────────────────────────────────────────────────────
# 排队预生成 (Prefetch) 缓存
# ─────────────────────────────────────────────────────────────
# 原理: 入队时 (前端钩住 app.queuePrompt) 立即后台调用 LLM 生成提示词并存入缓存;
#       该任务真正执行到提示词节点时, _maybe_api 命中缓存则直接返回, 跳过 API 调用。
#       适合「队列里还有任务在跑, 下一个任务的提示词先出」的场景, 把 LLM 耗时移出关键路径。
_PREGEN_VERSION = "h3promptgen-pregen-v1"
_PREGEN_CACHE = {}                     # key -> deque[(final, status)]
_PREGEN_CACHE_LOCK = threading.Lock()
_PREGEN_STORE = threading.local()      # 路由线程内置 .active=True → 成功后写缓存
_PREGEN_MAX_PER_KEY = 32
_PREGEN_MAX_KEYS = 512


def _pregen_key(system, prompt, api_url, temperature, top_p, max_tokens, seed_arg,
                api_key, model_name, image_mode=False, top_k=0, enable_thinking=False,
                reasoning_budget=512):
    """由「API 请求的完整决定因素」生成缓存键 —— 前端入队预生成与节点执行时
    走同一套 build 代码, 组装的 system/prompt 一致, 键必然一致。

    image_mode=True 时用独立命名空间: 带参考图 (多模态) 与纯文本请求即使参数
    相同也互不串用 (同一个节点先纯文本后带图, 或反过来, 都不会命中对方缓存)。
    """
    h = hashlib.sha256()
    h.update(_PREGEN_VERSION.encode("utf-8"))
    h.update(b"\x00img" if image_mode else b"\x00txt")
    for part in (system or "", prompt or "", api_url or "",
                 repr(temperature), repr(top_p), repr(top_k), repr(enable_thinking),
                 repr(reasoning_budget), repr(max_tokens), repr(seed_arg), api_key or "", model_name or ""):
        h.update(b"\x00")
        h.update(str(part).encode("utf-8", "replace"))
    return h.hexdigest()


def _append_pregen(key, entry, replace=False):
    """预生成结果入队 (FIFO, 每次入队一项; 同键多次入队 → 依次消费)。

    replace=True (带图预生成): 新结果【替换】旧结果 —— 参考图请求的键不含图片
    内容, 若用 FIFO 追加, 改图后重新排队会先消费到旧图的旧结果; 替换语义保证
    同键永远只保留最近一次预生成的结果, 改图重排后立即生效。
    """
    with _PREGEN_CACHE_LOCK:
        if key not in _PREGEN_CACHE:
            if len(_PREGEN_CACHE) >= _PREGEN_MAX_KEYS:
                _PREGEN_CACHE.pop(next(iter(_PREGEN_CACHE)))
            _PREGEN_CACHE[key] = collections.deque(maxlen=_PREGEN_MAX_PER_KEY)
        elif replace:
            _PREGEN_CACHE[key] = collections.deque(maxlen=_PREGEN_MAX_PER_KEY)
        _PREGEN_CACHE[key].append(entry)


def _consume_pregen(key):
    """取走最早的预生成结果; 无则返回 None (走正常 API 调用)。"""
    with _PREGEN_CACHE_LOCK:
        dq = _PREGEN_CACHE.get(key)
        if dq:
            return dq.popleft()
        return None

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
SYS_DIR = os.path.join(NODE_DIR, "system_prompts")
FALLBACK_FILE = os.path.join(NODE_DIR, "fallback_system.txt")

# 生成后校验用的禁词 (脸红及近义词)

MINI_FALLBACK = """You are an expert MiniMax H3 video prompt writer and shot designer. Convert the user's story brief into a precisely formatted H3 video generation prompt.
Output ONLY the final prompt, in English, starting DIRECTLY with the field labels. Write every spoken or sung line in Chinese inside <d>[中文] ...</d>; keep user-supplied lines verbatim. Singing uses `sings:` instead of `says:`.
Use this exact format:
integrated_multimodal_description: [Shot 1] <style>, <opening composition> ...
[Shot 2] At MM:SS.mmm, the camera cuts to ...

overall_soundscape: <ambient + physical sounds, 1-4 sentences>

non_diegetic_music: <instrumentation/tempo/dynamics, 1-3 sentences, or N/A>

Rules: design the shots and blocking instead of translating the brief (a 10-second clip uses 1–4 shots, and a single continuous shot is valid). [Shot 1] has no timestamp; later shots use strictly increasing cut times inside the video duration. Camera motion is written as natural English (motion type + amplitude + speed). Speakers use stable IDs (S1), (S2). A shout or a name called out is dialogue inside <d>, never prose. Never use 脸红 or any blush synonym (blush, flushed, rosy cheeks). Visible on-screen text goes in English double quotes. Use N/A where a section does not apply."""

MODE_OPTIONS = [
    "T2VA (文生视频)",
    "I2VA (首帧参考)",
    "FL2VA (首尾帧参考)",
    "L2VA (尾帧参考)",
    "Ref2VA (完整参考)",
]

# ---------------- 视觉风格 (与 XB_ToolBox XB_llamaMiniMaxPreset 一致) ----------------
_STYLES = [
    "不指定 / Unspecified",
    "电影感 / Cinematic", "实拍 / Live-action",
    "复古胶片 / Vintage film", "黑白电影 / Black & White",
    "纪录片 / Documentary",
    "极简广告 / Minimalist commercial",
    "微距摄影 / Macro photography",
    "航拍 / Aerial drone",
    "二维动画 / 2D-animated", "三维CG / 3D CG",
    "日系二次元 / Anime", "美式漫画 / American Comic",
    "皮克斯3D / Pixar-style 3D", "定格动画 / Stop-motion",
    "手绘发光 / Hand-drawn glow", "像素艺术 / Pixel art",
    "赛博朋克 / Cyberpunk", "蒸汽朋克 / Steampunk",
    "故障艺术 / Glitch art",
    "羊毛毡 / Wool felt", "折纸 / Origami",
    "水彩 / Watercolor", "粘土动画 / Claymation",
    "水墨 / Ink wash", "油画 / Oil painting",
    "纸艺拼贴 / Paper collage", "剪纸 / Paper cutout",
    "铅笔素描 / Pencil sketch", "浮世绘 / Ukiyo-e",
    "敦煌壁画 / Dunhuang Murals",
    "青花瓷 / Blue-white Porcelain",
    "工笔画 / Gongbi Painting",
    "皮影戏 / Shadow Puppetry",
    "中国风插画 / Chinese Illustration",
    "年画 / New Year Painting",
    "布艺 / Fabric Art",
    "蜡笔画 / Crayon drawing",
    "哥特萝莉 / Gothic Lolita",
    "小红书网红 / Xiaohongshu Aesthetic",
    "抖音网红 / Douyin Influencer",
    "美颜视频 / Beauty-filter Video",
    "清新滤镜 / Fresh filter",
]

_STYLE_HINTS = {
    "电影感 / Cinematic": "cinematic lighting with shallow depth of field, film grain, and professional color grading",
    "实拍 / Live-action": "photorealistic live-action footage with natural lighting and authentic set design",
    "复古胶片 / Vintage film": "vintage film stock with warm color grading, subtle grain, and nostalgic atmosphere",
    "黑白电影 / Black & White": "high-contrast black-and-white cinematography with dramatic shadows",
    "纪录片 / Documentary": "observational documentary style with natural handheld camera work and candid framing",
    "极简广告 / Minimalist commercial": "clean minimalist product cinematography with smooth dolly moves, soft even lighting, and uncluttered compositions",
    "微距摄影 / Macro photography": "extreme close-up macro lens with razor-thin depth of field, revealing fine textures and details",
    "航拍 / Aerial drone": "sweeping aerial drone shots with wide vistas, slow majestic reveals, and expansive landscape views",
    "二维动画 / 2D-animated": "traditional 2D hand-drawn animation with expressive line art and fluid character motion",
    "三维CG / 3D CG": "high-quality 3D rendering with realistic materials, global illumination, and smooth animation",
    "日系二次元 / Anime": "Japanese anime cel-shading with vibrant saturated colors, clean linework, and expressive character designs",
    "美式漫画 / American Comic": "American comic book style with bold black ink outlines, halftone dot shading, and dynamic compositions",
    "皮克斯3D / Pixar-style 3D": "Pixar-quality 3D with smooth curved surfaces, rich vibrant colors, expressive character animation, and polished lighting",
    "定格动画 / Stop-motion": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a stop-motion animation. Characters are handcrafted puppets with visible material textures, moving with tactile frame-by-frame stutter. Environments are miniature physical sets with real fabrics, painted backdrops, and practical lighting. EVERYTHING is a physical model under a camera.",
    "手绘发光 / Hand-drawn glow": "GLOBAL MATERIAL OVERRIDE — the entire visual world is rough hand-drawn line art on dark paper. Characters and environments are sketched with glowing neon-colored outlines that flicker and pulse organically. Light trails follow movement like afterimages. The world itself is a living drawing, every line redrawn in real-time.",
    "像素艺术 / Pixel art": "GLOBAL MATERIAL OVERRIDE — the entire visual world is built from visible pixel blocks. Characters, environments, water, fire, smoke, sky — EVERYTHING is composed of crisp square pixels with a limited retro color palette. Motion is frame-by-frame at low FPS with deliberate pixel-level changes. Particles are individual pixel dots. The pixel grid IS the universe.",
    "赛博朋克 / Cyberpunk": "high-contrast neon-lit cyberpunk cityscape with rain-slicked streets, holographic displays, and chrome cybernetics",
    "蒸汽朋克 / Steampunk": "intricate brass machinery and Victorian-era steam technology with copper pipes, gears, and sepia tones",
    "故障艺术 / Glitch art": "digital glitch distortion with RGB color channel split, scan lines, data corruption artifacts, and VHS noise",
    "羊毛毡 / Wool felt": "GLOBAL MATERIAL OVERRIDE — the entire visual world is handcrafted from fuzzy wool felt. Characters have soft felt textile bodies with visible fiber textures and stitched seams. Wind ripples through felt grass, felt water flows with fiber movement, felt clouds drift across a felt sky. Environments are sewn felt dioramas. DO NOT place felt toys in a real scene — everything IS felt.",
    "折纸 / Origami": "GLOBAL MATERIAL OVERRIDE — the entire universe is constructed from folded paper. Characters are origami figures with sharp clean creases and geometric folded anatomy. Paper birds flap creased wings, paper water ripples in folded layers, paper fire crackles as curling sheets. The world itself is paper — all matter is folded, creased, and crisp.",
    "水彩 / Watercolor": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D watercolor painting on paper. Characters are NOT real people — their bodies are translucent color washes, their faces are soft pigment blooms on wet paper, their edges dissolve into the paper grain. Hair is bleeding pigment streaks, skin is the white of paper with tinted wash. Rain falls as pigment droplets, light diffuses through layered washes. NO realistic skin, NO 3D — only wet pigment on paper.",
    "粘土动画 / Claymation": "GLOBAL MATERIAL OVERRIDE — the entire visual world is hand-sculpted clay. Characters are NOT real people — their bodies are clay with rounded tactile surfaces, visible fingerprints, and tool marks. Hair is sculpted clay strands, skin is smooth plasticine, clothing is pressed clay sheets. Clay water splashes in sculpted droplets, clay smoke rolls in malleable puffs. NO real skin — only clay shaped by human hands.",
    "水墨 / Ink wash": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D ink wash painting on xuan rice paper. Characters are NOT real people — their bodies are fluid black brushstrokes, their faces are ink lines on paper, their clothing is graded ink washes. Hair flows as sweeping brushstrokes, skin tone is the white of the paper itself with ink shading. Water splashes as flying ink drops, wind leaves brushstroke trails, mist is spreading ink on wet paper. NO realistic skin, NO realistic fabric, NO 3D — only ink and paper.",
    "油画 / Oil painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D oil painting on canvas. Characters are NOT real people — their bodies are thick oil paint applied with palette knives, their faces are built from layered brushstrokes, their clothing is impasto pigment. Hair is swept paint, skin is blended oil color on canvas, eyes are precise brush dabs. Water ripples in heavy oil strokes, fire is palette-knife texture, clouds are smeared white paint. NO realistic skin, NO real fabric, NO 3D — only oil paint on canvas.",
    "纸艺拼贴 / Paper collage": "GLOBAL MATERIAL OVERRIDE — the entire visual world is layered torn paper. Characters are NOT real people — their bodies are cut from textured paper with torn edges, their faces are printed paper fragments, their clothing is different paper types (newsprint, craft, tissue). Paper birds flap torn-edge wings, paper water ripples in layered sheets. NO real skin — only paper.",
    "剪纸 / Paper cutout": "GLOBAL MATERIAL OVERRIDE — the entire visual world is Chinese paper cutout art. Characters are NOT real people — their bodies are intricate red paper silhouettes cut with symmetrical patterns, moving with articulated paper joints. Shadows cast dramatic shapes through the paper lattice. NO real skin — only cut paper.",
    "铅笔素描 / Pencil sketch": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D graphite pencil drawing on textured paper. Characters are NOT real people — their bodies are graphite lines, hatching, and cross-hatching on paper. Faces are sketched pencil marks, hair is sweeping graphite strokes, skin tone is the white of paper with varying pencil pressure. Motion is lines erasing and redrawing. Eraser marks leave ghost trails. NO real skin, NO 3D — only pencil on paper.",
    "浮世绘 / Ukiyo-e": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D Japanese ukiyo-e woodblock print. Characters are NOT real people — their bodies are flat color areas with bold black outlines printed on washi paper. Faces are printed woodblock features, hair is carved-line black ink, clothing is flat color blocks. NO real skin, NO 3D — only woodblock ink on paper.",
    "敦煌壁画 / Dunhuang Murals": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D animated Dunhuang cave mural on a fresco wall. Characters are NOT real people — their bodies are mineral pigment paintings (ochre, turquoise, lapis lazuli) with weathered fresco cracks. Flying deities trail faded pigment ribbons. NO real skin, NO 3D — only ancient mural pigment on plaster.",
    "青花瓷 / Blue-white Porcelain": "GLOBAL MATERIAL OVERRIDE — the entire universe is living 3D blue-and-white porcelain. Characters are NOT real people — their bodies are white-glazed porcelain with cobalt-blue hand-painted patterns flowing across their skin as features and clothing. Porcelain birds take flight with clicking ceramic wings, porcelain water flows as liquid glaze, porcelain trees bloom with cobalt flowers. NO real skin — only glazed ceramic.",
    "工笔画 / Gongbi Painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D gongbi painting on flat silk. Characters are NOT real people — their bodies are ultra-fine brush outlines filled with flat mineral color washes on silk. Every hair and petal is individually painted. Silk fibers visible beneath the pigment. NO real skin, NO 3D — only brush and silk.",
    "皮影戏 / Shadow Puppetry": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D shadow puppet theater on a translucent screen. Characters are NOT real people — their bodies are intricately carved leather silhouettes with articulated joints, illuminated by warm amber backlighting. NO real skin, NO 3D — only leather shadows on a screen.",
    "中国风插画 / Chinese Illustration": "modern Chinese illustration blending traditional ink aesthetics with contemporary digital art, featuring elegant flowing lines, poetic composition, and dreamlike color harmony",
    "年画 / New Year Painting": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D vibrant Chinese folk New Year woodblock print. Characters are NOT real people — their bodies are bold primary color blocks with thick black outlines on flat printed paper. Door gods step out of frames and walk, carp leap as printed patterns. NO real skin, NO 3D — only folk print on paper.",
    "布艺 / Fabric Art": "GLOBAL MATERIAL OVERRIDE — the entire visual world is constructed from sewn fabric and textiles. Characters are NOT real people — they are cloth dolls with stitched seams, button eyes, embroidered facial features, yarn hair, and patchwork clothing. Environments are quilted fabric landscapes: grass is green felt, water is flowing blue silk, clouds are tufted cotton, trees are embroidered tapestry. Every surface shows visible thread, stitching, and fabric grain. NO realistic skin, NO real materials — only fabric and thread.",
    "蜡笔画 / Crayon drawing": "GLOBAL MATERIAL OVERRIDE — the entire visual world is a 2D wax crayon drawing on textured paper. Characters are NOT real people — their bodies are waxy crayon strokes on paper with the paper grain visible through the wax. Bright colors have the distinctive grainy, slightly uneven crayon texture. Lines are thick and waxy with visible stroke direction. Paper texture shows through all color areas. NO 3D depth, NO CG, NO realistic skin — only crayon on paper.",
    "哥特萝莉 / Gothic Lolita": "Gothic Lolita fashion and atmosphere — NOT a material override but a costume and world style. Characters wear elaborate dark Victorian-inspired Lolita clothing: lace-trimmed black dresses, ruffled petticoats, corsets, platform boots, ornate headpieces with ribbons and roses. Architecture is moody Gothic with pointed arches, stained glass, wrought iron. Dramatic chiaroscuro lighting with deep shadows. Color palette: black, deep purple, burgundy, ivory, silver accents. Atmosphere is darkly romantic and theatrical.",
    "小红书网红 / Xiaohongshu Aesthetic": "Xiaohongshu (RED) lifestyle aesthetic: polished social-media lifestyle photography with bright airy lighting, clean modern interiors or pretty cafe/scenic spots, subjects with flattering soft-skin glow, high-saturation vibrant colors, slightly warm color grade with a subtle pink-coral tone, composed like a carefully styled photo shoot — subjects aware of being photographed, poses slightly performatively candid, everything framed as aspirational 'good life' content",
    "抖音网红 / Douyin Influencer": "Douyin/TikTok influencer style: hyper-energetic short-form video look with punchy saturated colors, dramatic or glowing lighting, fast-paced dynamic framing, exaggerated reactions and big smiles, strong hook energy, background music energy visible in the body language, subjects look straight into camera with confident social-media charisma",
    "美颜视频 / Beauty-filter Video": "beauty-filter video aesthetic: ultra-smooth airbrushed skin with porcelain glow, softened features, even flattering lighting that removes all texture, slightly over-saturated pastel color palette, dreamy soft-focus background bokeh, the overall look of a video shot through a 'beauty mode' camera filter — flawless, glowing, slightly hyper-real on faces",
    "清新滤镜 / Fresh filter": "fresh, clean filter look: high-key bright lighting with a soft low-contrast haze, pale mint/cream/sky-blue color palette, airy white balance with a slightly cool-to-neutral tint, dewy natural subjects, light and weightless atmosphere like a summer lifestyle ad, clean minimal composition with lots of bright open space",
}

# ---------------- 音乐风格 (与 XB_ToolBox XB_llamaMiniMaxPreset 一致) ----------------
_MUSIC = [
    "禁止音乐 / No Music",
    "不指定 / Unspecified",
    "钢琴 / Piano", "管弦乐 / Orchestral",
    "原声吉他 / Acoustic",
    "电子 / Electronic", "氛围 / Ambient",
    "合成器浪潮 / Synthwave", "芯片音乐 / Chiptune",
    "Lo-fi / Lo-fi",
    "史诗 / Epic", "悬疑 / Suspense",
    "浪漫弦乐 / Romantic Strings",
    "摇滚 / Rock", "爵士 / Jazz",
    "嘻哈 / Hip-Hop", "放克 / Funk",
    "纯人声合唱 / Acapella Choir",
    "极简拟音 / Minimalist Foley",
    "国风民乐 / Chinese Folk",
    "戏曲 / Chinese Opera",
    "古琴 / Guqin",
]

_MUSIC_HINTS = {
    "禁止音乐 / No Music": "ABSOLUTELY NO background music of any kind. non_diegetic_music MUST be \"N/A\". Do not add any score, melody, or rhythm.",
    "钢琴 / Piano": "a solo piano piece at a slow to moderate tempo, with sparse delicate notes and natural reverb",
    "管弦乐 / Orchestral": "a majestic full orchestral arrangement with swelling strings and warm brass, maintaining a steady high-energy rhythm throughout",
    "原声吉他 / Acoustic": "an acoustic guitar piece with gentle fingerpicking patterns and warm natural wood resonance",
    "电子 / Electronic": "an electronic track with layered synthesizers, digital beats, and atmospheric pads",
    "氛围 / Ambient": "a minimal ambient soundscape with long sustained tones, subtle textures, and no distinct rhythm",
    "合成器浪潮 / Synthwave": "a pulsing Synthwave instrumental track with heavy analog bass, retro drum machines, neon-soaked pads, and a driving steady rhythm, no vocals, starting abruptly at full energy with zero intro",
    "芯片音乐 / Chiptune": "a retro 8-bit instrumental chiptune track with square-wave melodies, simple waveforms, and nostalgic video game sound",
    "Lo-fi / Lo-fi": "a lo-fi instrumental beat with vinyl crackle, mellow chords, soft drum loops, and a relaxed downtempo groove",
    "史诗 / Epic": "an epic cinematic instrumental score with powerful brass, thundering percussion, soaring choir, and dramatic steady intensity, starting abruptly at full energy without any intro or build-up",
    "悬疑 / Suspense": "a tense suspense instrumental score with low-frequency drones, sudden dissonant stabs, creeping tension, and unsettling silence",
    "浪漫弦乐 / Romantic Strings": "a romantic instrumental string arrangement with lush violins, gentle cello, harp glissandos, and a tender sustained atmosphere",
    "摇滚 / Rock": "an instrumental-only rock track with electric guitar riffs, driving drums, bass groove, and energetic dynamics, STRICTLY NO VOCALS, starting at full power with zero intro",
    "爵士 / Jazz": "an instrumental jazz piece with walking bass, brushed drums, improvisational piano or saxophone, smoky club atmosphere, no vocals",
    "嘻哈 / Hip-Hop": "an instrumental hip-hop beat with heavy 808 bass, crisp trap snares, hi-hat rolls, and a grooving rhythmic flow, STRICTLY NO VOCALS, dropping in at full energy with no intro",
    "放克 / Funk": "an instrumental funk groove with a bouncy slap bassline, tight rhythm guitar, brass stabs, and an infectious syncopated rhythm, no vocals, kicking in immediately at full groove",
    "纯人声合唱 / Acapella Choir": "a pure acapella choir with layered vocal harmonies and no instruments, evoking sacred, ethereal, or haunting atmosphere",
    "极简拟音 / Minimalist Foley": "minimalist foley and ambient silence — only crisp physical sound effects like subtle clicks, soft whooshes, and spatial emptiness, with no melodic music at all",
    "国风民乐 / Chinese Folk": "a traditional Chinese folk piece with guzheng, erhu, dizi bamboo flute, pipa, and flowing pentatonic melodies evoking ancient landscapes",
    "戏曲 / Chinese Opera": "a stylized Chinese opera piece with clanging gongs, wooden clappers, piercing erhu, and dramatic vocal delivery in traditional theatrical style",
    "古琴 / Guqin": "a solo guqin piece with deep resonant plucked silk strings, slow meditative pace, profound stillness, and subtle harmonic overtones",
}


# ---------------- Krea-2 画面风格 (下拉, 强化风格描述; 遵循 XB_ToolBox KREA2 规范) ----------------
# 每条 hint 都以画质/风格锚定词开头 (Krea-2 前 15 token 注意力最高), 逗号分隔直述,
# 不用括号权重语法; 选中后该段英文描述会被前置注入提示词开头。
_KREA2_STYLES = [
    "不指定 / Unspecified",
    "极致写实摄影 / Hyper-Realistic Photography",
    "电影感 / Cinematic",
    "商业广告 / Commercial Advertising",
    "复古胶片 / Vintage Film",
    "黑白艺术 / Black & White",
    "微距摄影 / Macro Photography",
    "航拍 / Aerial Drone",
    "森系清新 / Fresh Pastel",
    "小红书网红 / Xiaohongshu Aesthetic",
    "抖音网红 / Douyin Influencer",
    "美颜写真 / Beauty-Filter Portrait",
    "三维渲染 / 3D Render",
    "皮克斯3D / Pixar-style 3D",
    "低多边形 / Low Poly",
    "日系二次元 / Anime",
    "美式漫画 / American Comic",
    "国漫插画 / Chinese Animation",
    "像素艺术 / Pixel Art",
    "手绘插画 / Hand-Drawn Illustration",
    "水彩 / Watercolor",
    "水墨国风 / Ink Wash",
    "油画 / Oil Painting",
    "浮世绘 / Ukiyo-e",
    "敦煌壁画 / Dunhuang Murals",
    "剪纸 / Paper Cutout",
    "赛博朋克 / Cyberpunk",
    "蒸汽朋克 / Steampunk",
    "梦幻幻想 / Dreamy Fantasy",
    "哥特暗黑 / Dark Gothic",
    "极简主义 / Minimalist",
    "玻璃质感 / Glassmorphism",
]

_KREA2_STYLE_HINTS = {
    "极致写实摄影 / Hyper-Realistic Photography":
        "Hyper-realistic professional photography, 8K resolution, 85mm f/1.4 lens, shallow depth of field, "
        "natural skin texture with visible pores, physically accurate global illumination, cinematic color grading, "
        "ultra sharp focus, magazine cover quality",
    "电影感 / Cinematic":
        "Cinematic film still, anamorphic lens, dramatic volumetric lighting, subtle film grain, "
        "teal-and-orange color grading, shallow depth of field, professional movie production quality, 8K",
    "商业广告 / Commercial Advertising":
        "Premium commercial advertising photography, studio softbox lighting, flawlessly lit subject, "
        "high-end brand campaign aesthetic, ultra sharp focus, rich contrast, 8K resolution, luxury product showcase",
    "复古胶片 / Vintage Film":
        "Vintage 35mm film photography, warm faded color palette, subtle film grain, gentle halation highlights, "
        "Kodak Portra tones, nostalgic analog atmosphere, timeless retro mood",
    "黑白艺术 / Black & White":
        "High-contrast black-and-white fine art photography, dramatic chiaroscuro lighting, deep velvety shadows, "
        "silver halide grain, timeless monochrome elegance, masterful composition",
    "微距摄影 / Macro Photography":
        "Extreme macro photography, razor-thin depth of field, intricate surface textures, glistening micro details, "
        "professional macro lens, creamy bokeh background, 8K detail",
    "航拍 / Aerial Drone":
        "Breathtaking aerial drone photography, sweeping landscape vista, golden hour light, ultra-detailed terrain, "
        "cinematic scale, expansive composition, 8K resolution",
    "森系清新 / Fresh Pastel":
        "Airy fresh pastel aesthetic, soft diffused daylight, pale mint and cream palette, clean minimalist composition, "
        "dewy natural beauty, weightless summer atmosphere, high-key bright lighting",
    "小红书网红 / Xiaohongshu Aesthetic":
        "Xiaohongshu lifestyle photography, bright airy lighting, polished soft-skin glow, high-saturation vibrant colors, "
        "warm coral-pink color grade, aspirational social-media composition, carefully styled scene",
    "抖音网红 / Douyin Influencer":
        "Douyin influencer photo style, punchy saturated colors, glowing glamorous lighting, energetic confident charisma, "
        "trendy fashionable outfit, social-media star look, eye-catching dynamic energy",
    "美颜写真 / Beauty-Filter Portrait":
        "Beauty-filter portrait photography, ultra-smooth porcelain skin with glowing complexion, soft dreamy focus, "
        "flattering even lighting, flawless airbrushed look, gentle pastel tones, high-key beauty selfie quality",
    "三维渲染 / 3D Render":
        "High-end 3D render, Octane render, physically based materials, global illumination, ray-traced reflections, "
        "subsurface scattering, cinematic depth of field, ultra detailed, 8K",
    "皮克斯3D / Pixar-style 3D":
        "Pixar-quality 3D animation still, smooth curved surfaces, expressive stylized characters, "
        "vibrant saturated colors, polished studio lighting, heartwarming charm, high-end CGI render",
    "低多边形 / Low Poly":
        "Low poly 3D art, faceted geometric surfaces, flat-shaded vibrant colors, stylized minimal aesthetic, "
        "clean crisp composition, playful indie game look",
    "日系二次元 / Anime":
        "Japanese anime illustration, cel shading, clean bold linework, vibrant saturated colors, "
        "detailed expressive eyes, dynamic composition, high-quality key visual, anime poster art",
    "美式漫画 / American Comic":
        "American comic book art, bold black ink outlines, halftone dot shading, dramatic dynamic poses, "
        "vibrant primary colors, superhero poster energy, action-packed composition",
    "国漫插画 / Chinese Animation":
        "Modern Chinese animation illustration, refined painterly rendering, elegant flowing lines, "
        "poetic atmosphere, dreamlike color harmony, delicate detailed background art",
    "像素艺术 / Pixel Art":
        "Retro pixel art, crisp square pixels, limited classic palette, detailed sprite work, "
        "nostalgic 16-bit game aesthetic, charming low-res charm",
    "手绘插画 / Hand-Drawn Illustration":
        "Hand-drawn digital illustration, visible pencil and brush strokes, textured paper feel, "
        "expressive artistic linework, warm artistic character, storybook charm",
    "水彩 / Watercolor":
        "Delicate watercolor painting, translucent pigment washes, soft color bleeding on paper, "
        "visible paper grain, gentle artistic atmosphere, hand-painted charm",
    "水墨国风 / Ink Wash":
        "Traditional Chinese ink wash painting, flowing black brushstrokes on xuan paper, graded ink tones, "
        "poetic negative space, oriental elegance, timeless brush art",
    "油画 / Oil Painting":
        "Classical oil painting, thick impasto brushstrokes, rich layered pigments, dramatic Baroque lighting, "
        "museum masterpiece quality, textured canvas surface",
    "浮世绘 / Ukiyo-e":
        "Japanese ukiyo-e woodblock print, flat color blocks with bold outlines, washi paper texture, "
        "traditional Edo-period aesthetic, hand-printed art style",
    "敦煌壁画 / Dunhuang Murals":
        "Dunhuang cave mural fresco, mineral pigment colors, weathered plaster texture, "
        "ochre turquoise and lapis hues, ancient Buddhist art, timeless fresco beauty",
    "剪纸 / Paper Cutout":
        "Chinese paper cutout art, intricate red paper silhouettes, symmetrical patterns, layered paper depth, "
        "folk art charm, hand-cut detail",
    "赛博朋克 / Cyberpunk":
        "Cyberpunk neon cityscape, rain-slicked streets, holographic advertisements, chrome cybernetics, "
        "magenta and cyan neon rim lighting, cinematic haze, futuristic dystopian atmosphere",
    "蒸汽朋克 / Steampunk":
        "Steampunk machinery, intricate brass and copper gears, Victorian-era design, warm sepia tones, "
        "steam and clockwork details, antique mechanical beauty",
    "梦幻幻想 / Dreamy Fantasy":
        "Dreamy fantasy art, ethereal glowing light, floating magical particles, soft pastel palette, "
        "epic scale, enchanted atmosphere, painterly ethereal beauty",
    "哥特暗黑 / Dark Gothic":
        "Dark gothic aesthetic, moody chiaroscuro lighting, ornate Victorian architecture, "
        "deep blacks with burgundy and silver accents, theatrical atmosphere, mysterious elegance",
    "极简主义 / Minimalist":
        "Minimalist art, clean negative space, single bold subject, muted sophisticated palette, "
        "balanced geometric composition, high-end abstract elegance, serene simplicity",
    "玻璃质感 / Glassmorphism":
        "Glassmorphism style, translucent frosted glass surfaces, soft refractions, iridescent light caustics, "
        "clean modern 3D aesthetic, luminous ethereal glow, futuristic UI-inspired art",
}

# 写实增强尾巴 (取自用户 Krea-2 工作流正向提示词, 已被验证有效): 仅对摄影写实类风格生效
_REALISM_TAIL = ("realistic facial feature proportions, realistic body proportions, authentic hair texture, "
                 "genuine clothing material, real-world environment")
_PHOTOREAL_STYLES = {
    "极致写实摄影 / Hyper-Realistic Photography",
    "电影感 / Cinematic",
    "商业广告 / Commercial Advertising",
    "复古胶片 / Vintage Film",
    "黑白艺术 / Black & White",
    "微距摄影 / Macro Photography",
    "航拍 / Aerial Drone",
    "森系清新 / Fresh Pastel",
    "小红书网红 / Xiaohongshu Aesthetic",
    "抖音网红 / Douyin Influencer",
    "美颜写真 / Beauty-Filter Portrait",
}


def _mode_key(label):
    return label.split(" ")[0].lower()


# 多段剧情分隔符处理: 全程使用 chr() 构造换行/连字符, 不写任何反斜杠转义, 避免工具误转义。
# 每段描述独占一行或连续若干行; 分隔线 = 去空白后"全部由 3 个及以上 ASCII 连字符(-, U+002D)组成"。
# 容错: (a) 单独一行形如 "剧情组 1" / "剧情组2" 的标记行也视作分隔;
#       (b) 用户可能输入字面量 "\\n" (反斜杠+n), 同样识别为换行; 也兼容 \\r\\n。
_NL = chr(10)
_CR = chr(13)
_DASH = chr(45)  # ASCII hyphen-minus '-'
_BACKSLASH = chr(92)  # ASCII backslash '\'  (用于检测用户输入的 literal "\\n")


def _norm_newlines(text):
    """把字面量反斜杠-n 也变成真实换行, 兼容不同来源的 user_prompt。"""
    # 用户可能输入字面量 backslash-n (2 个字符), 也视为换行
    text = text.replace(_BACKSLASH + "n", _NL)
    return text.replace(_CR + _NL, _NL)


def _is_separator_line(stripped):
    """分隔线判定: 去空白后要么全部由 3+ 个 ASCII 连字符组成, 要么形如'剧情组 N'。"""
    if not stripped:
        return False
    if all(c == _DASH for c in stripped) and len(stripped) >= 3:
        return True
    # "剧情组 N" 标记行 (N 为数字), 如 "剧情组 1" / "剧情组2" / "剧情组=3"
    head = stripped.replace(" ", "")
    if head.startswith("剧情组") and len(head) > 2:
        rest = head[2:].lstrip("=：:")
        return rest.isdigit() and len(rest) >= 1
    return False


def _join_lines(lines):
    """用真实换行连接若干行 (避免反斜杠转义出现在源码中)。"""
    return _NL.join(lines)


class H3PromptGenerator:
    """方案2: 纯提示词工程 — 输出 system_prompt + user_prompt 给 llama-cpp-vlm 节点。

    单段 (group_count == 1): 行为与原节点完全一致 —— 输出 1 组 H3 提示词。
    多段 (group_count >= 2): 多段剧情拼接生成长视频提示词 —— user_prompt 内每段一段
    描述 (用 --- 分隔), 本节点生成一个专门 system prompt, 指令 vlm 节点产出 N 组
    完整 H3 提示词, 且第 i+1 组的画面与情节必须衔接第 i 组的结尾 (同一角色/场景/视觉风格
    连续、承接上一组末帧动作、保留未解决的悬念钩子), 以此类推可生成任意多组。
    真正的多组生成由 llama-cpp_vlm 完成, 本节点只负责拼装 system/user prompt。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (MODE_OPTIONS,),
                "group_count": ("INT", {
                    "default": 1, "min": 1, "max": 99, "step": 1,
                    "tooltip": "剧情组数 / 拼接段数。1 = 单段; N(≥2) 时在 user_prompt 内用单独一行『---』分隔 N 段剧情, "
                               "输出 N 组完整且互相衔接的 H3 提示词 (每组 = 一段 10s 视频)。Ref2VA 每组六字段 "
                               "(subject_definitions/summary/retention_analysis/detailed_description/"
                               "overall_soundscape/non_diegetic_music), T2VA 每组三字段; 每组都重写全部字段, "
                               "长视频由这 N 组依次拼接而成。",
                }),
                "user_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "只写剧情和台词即可, 镜头机位/人物动作/分镜时间由 AI 设计。多段拼接时每组一段, "
                                   "用单独一行『---』分隔。示例:\n\n"
                                   "深夜出租屋, 阿强把一沓钱拍在桌上:『钱我凑齐了, 明天你就走。』\n"
                                   "---\n"
                                   "阿珍数完钱发现少了一半, 把钱摔在他脸上:『少了三千, 你他妈又拿去赌了?』\n"
                                   "---\n"
                                   "阿强低头点烟, 阿珍抓起外套冲进雨里, 他在后面喊:『阿珍!』\n\n"
                                   "(台词一律输出中文; 单段模式可只写一段, 无需分隔符; I2VA/FL2VA/L2VA/Ref2VA "
                                   "可留空只传参考图给 vlm 节点)",
                }),
                "duration_seconds": ("INT", {
                    "default": 10, "min": 4, "max": 15, "step": 1,
                    "tooltip": "每组视频时长 (秒), MiniMax H3 支持 4–15 秒",
                }),
                "视觉风格": (_STYLES, {"default": "不指定 / Unspecified"}),
                "音乐风格": (_MUSIC, {"default": "禁止音乐 / No Music"}),
                "use_api": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "是否直接调用 API 服务器生成 H3 提示词。选择 yes 且 api_url 填了正确的 "
                               "OpenAI 兼容地址时, final_prompt 输出口直接产出提示词; 否则 final_prompt 输出空字符串, "
                               "只通过 api_status 提示原因, 不报错。",
                }),
                "api_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "OpenAI 兼容 API 地址, 如 http://localhost:8080/v1",
                    "tooltip": "OpenAI 兼容的 /v1 地址 (llama.cpp server 等)。use_api=yes 且此地址可达时, "
                               "final_prompt 直接产出 H3 提示词; 地址错误/为空时 final_prompt 为空, api_status 提示原因。"
                               "可省略 http:// 前缀 (自动补全)。",
                }),
                "use_random_seed": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "随机种子: yes=每次生成结果不同 (不传 seed 给 API); no=固定使用下方 seed 值, 结果可复现。",
                }),
                "seed": ("INT", {
                    "default": 42, "min": 0, "max": 4294967295, "step": 1,
                    "tooltip": "固定种子值 (use_random_seed=no 时生效)。llama.cpp 支持 0–4294967295。",
                }),
            },
            "optional": {
                "temperature": ("FLOAT", {
                    "default": 0.9, "min": 0.0, "max": 2.0, "step": 0.05,
                    "tooltip": "采样温度。Qwen3.6 常用 0.9；越低越稳、越高越长。",
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "核采样 top_p。Qwen3.6 常用 0.95。",
                }),
                "top_k": ("INT", {
                    "default": 40, "min": 0, "max": 200, "step": 1,
                    "tooltip": "top_k 采样。Qwen3.6 常用 40；0 = 不启用。",
                }),
                "max_tokens": ("INT", {
                    "default": 8192, "min": 256, "max": 32768, "step": 256,
                    "tooltip": "输出 token 上限（不是上下文）。3–5 组一般 8192 足够；组数很多或开思考时调到 12288–16384。"
                               "上限受服务端 -c 约束，不要超过上下文。",
                }),
                "enable_thinking": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Qwen3 思考模式：开启=先推理再输出（质量更稳但慢数倍，且可能吃满 max_tokens）。需配合 reasoning_budget；批量出稿建议关闭。",
                }),
                "reasoning_budget": ("INT", {
                    "default": 512, "min": -1, "max": 8192, "step": 128,
                    "tooltip": "思考 token 预算（仅 enable_thinking=yes 时生效）：-1 = 不限、0 = 等同关闭；"
                               "中等思考约 512–1024。llama.cpp 的 reasoning_budget 参数实测有效。",
                }),
                "system_extra": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "追加到 system prompt 的自定义规则 (可选)",
                }),
                "image": ("IMAGE", {
                    "tooltip": "参考图 (可选)。use_api=yes 时按 OpenAI 多模态格式传给 API 服务器 "
                               "(I2VA 首帧 / FL2VA 首尾帧 / L2VA 尾帧 / Ref2VA 多张, 多张用 batch 接入)。"
                               "use_api=no 时本输入无影响 (参考图照旧接 vlm 节点的 images)。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("system_prompt", "user_prompt", "final_prompt", "api_status")
    FUNCTION = "build"
    CATEGORY = "H3/提示词生成"

    # ---------- 多段分割工具 ----------

    @staticmethod
    def _split_segments(user_prompt):
        """把 raw user_prompt 按分隔符切成段落列表, 去空白, 保留顺序。

        分隔判定: 某行去空白后要么「全部由 3+ 个 ASCII 连字符(-)组成」,
        要么形如「剧情组 N」标记行。也兼容用户输入字面量 backslash-n。
        """
        text = _norm_newlines(user_prompt or "")
        # _norm_newlines 已把字面量 backslash-n 与 CRLF 都变成真实换行 (_NL); 直接用 _NL 分割
        lines = text.split(_NL)
        segments = []
        current = []
        for line in lines:
            stripped = line.strip()
            if stripped and _is_separator_line(stripped):
                if current:
                    segments.append(_join_lines(current).strip())
                    current = []
            elif stripped:
                current.append(stripped)
        if current:
            segments.append(_join_lines(current).strip())
        return [s for s in segments if s]

    # ---------- 内部工具 ----------

    def _load_system_prompt(self, mode_key):
        """优先级: 模式专项文件 > fallback_system.txt > 内置精简版"""
        p = os.path.join(SYS_DIR, mode_key + ".txt")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        if os.path.exists(FALLBACK_FILE):
            with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                return f.read()
        return MINI_FALLBACK

    def _build_param_block(self, duration_seconds, style, music, mode_key="t2va", groups=1):
        """时长 + 视觉风格 + 音乐风格 → 参数块 (英文, 与 XB_ToolBox 注入格式一致)。

        多组时说明「每组 N 秒、拼接后总时长」; Ref2VA 的风格开场在 [Shot 1] 之前,
        其余模式的 [Shot 1] 直接以风格名开头 (官方指南两种写法的差别)。
        """
        lines = ["## Target Video Parameters (MUST follow exactly)"]
        if groups > 1:
            lines.append(
                f"- Duration: exactly {duration_seconds} seconds PER GROUP ({groups} groups, about "
                f"{duration_seconds * groups} seconds when concatenated). Inside every group all shot "
                f"timestamps MUST fall within 0.00–{duration_seconds}.00 s and that group's final shot "
                f"must end before the {duration_seconds}-second mark."
            )
        else:
            lines.append(
                f"- Duration: exactly {duration_seconds} seconds (all shot timestamps MUST fall "
                f"within this range; the final shot must end before the {duration_seconds}-second mark)"
            )
        if "Unspecified" not in style:
            en_name = style.split(" / ")[-1]  # "中文 / English" → English
            hint = _STYLE_HINTS.get(style, "")
            lines.append(f"- Visual style: {en_name} — {hint}")
            if mode_key == "ref2va":
                lines.append(
                    f'  ⚠️ Open detailed_description with 1-2 English sentences that establish "{en_name}" '
                    f'and show what it looks like — textures, lighting, colors, motion characteristics — '
                    f'BEFORE [Shot 1]. [Shot 1] follows immediately after and must not repeat the style name.'
                )
            else:
                lines.append(
                    f'  ⚠️ [Shot 1] MUST begin with "{en_name}" and immediately elaborate with 1-2 sentences '
                    f'of concrete visual description — textures, lighting, colors, motion characteristics that '
                    f'define this style. Do NOT just name the style and move on. Show what the style actually looks like.'
                )
                lines.append(
                    f'  Correct: "[Shot 1] {en_name}, the characters have the rounded tactile quality of '
                    f'hand-sculpted clay with visible fingerprints and tool marks..."'
                )
                lines.append(
                    f'  Wrong: "[Shot 1] {en_name}, a medium-wide shot..." (name only, no visual description)'
                )
        if "Unspecified" not in music:
            en_name = music.split(" / ")[-1]  # "中文 / English" → English
            hint = _MUSIC_HINTS.get(music, "")
            lines.append(f"- Background music style: {en_name} — {hint}")
            if "No Music" in music:
                lines.append(
                    '  ⚠️ The video must have absolutely no background music. non_diegetic_music MUST be "N/A".'
                )
        return "\n".join(lines)

    # ---------- 多段拼接规则注入 ----------

    @staticmethod
    def _image_count(image):
        """统计本次请求实际附带的参考图数量 (torch/numpy [B,H,W,C] / list / PIL)。"""
        if image is None:
            return 0
        if isinstance(image, (list, tuple)):
            return len(image)
        shape = getattr(image, "shape", None)
        if shape is not None and len(shape) == 4:
            return int(shape[0])
        return 1

    def _asset_notice(self, image):
        """告诉 Ref2VA 模型实际附带了几张图, 避免它虚构 <Picture 3> 或引用不存在的参考图。"""
        n = self._image_count(image)
        if n <= 0:
            return ("# Attached assets\nNo reference image is attached to this request: there is no <Picture N>, "
                    "describe every subject in words.")
        pics = ", ".join("<Picture %d>" % i for i in range(1, n + 1))
        return ("# Attached assets\nExactly %d reference image(s) are attached: %s. No other <Picture N> exists. "
                "A room or prop without its own image is a <Subject N> described in words." % (n, pics))

    @staticmethod
    def _group_skeleton(mode_key, duration_seconds):
        """每组的字段骨架 (按模式): Ref2VA 六段, 基础模式三段 (关键帧模式带对齐行)。"""
        d = str(duration_seconds)
        if mode_key == "ref2va":
            return [
                "  ### Group k",
                "  subject_definitions:",
                "  <Subject 1> is the woman in <Picture 1> — repeat this citation in EVERY group — with ... (or her full text description when no image is attached), and how she looks at the START of Group k",
                "  <Subject 2> is ...",
                "  summary:",
                "  [reference generation] Group k continues from the end of Group k-1: ...",
                "  retention_analysis:",
                "  <Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - ...",
                "  <Subject 2> (appears in [Shot 1]): fully_preserved - ...",
                "  detailed_description:",
                "  <one or two style sentences>",
                "  [Shot 1] ...   (write the full visual detail: framing, blocking, action, environment/light, props, reaction, sound)",
                "  [Shot 2] At 00:02.700, ...   (the time is only an example — use the moment this beat's action ends; a single continuous shot is also fine)",
                "  overall_soundscape:",
                "  ...",
                "  non_diegetic_music:",
                "  ...",
            ]
        if mode_key == "i2va":
            return [
                "  ### Group 1",
                "  For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.",
                "",
                "  integrated_multimodal_description: [Shot 1] ...",
                "  [Shot 2] At 00:02.700, ...   (the time is only an example — use the moment this beat's action ends; a single continuous shot is also fine)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
                "",
                "  ### Group 2 ... Group N",
                "  integrated_multimodal_description: [Shot 1] ...   (no alignment line: opens exactly on the final frame of the previous group)",
                "  [Shot 2] At 00:02.700, ...   (the time is only an example — use the moment this beat's action ends; a single continuous shot is also fine)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
            ]
        if mode_key == "fl2va":
            return [
                "  ### Group 1",
                "  How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video. (Picture 2 is the last frame of Group N, not of Group 1.)",
                "",
                "  integrated_multimodal_description: [Shot 1] ...",
                "  [Shot 2] At 00:02.700, ...   (the time is only an example — use the moment this beat's action ends; a single continuous shot is also fine)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
                "",
                "  ### Group N",
                "  How the reference pictures align with the target video — Picture 2 (from Shot M) aligns with the " + d + ".00-second mark of the target video.",
                "",
                "  integrated_multimodal_description: ...   (the last shot lands exactly on Picture 2)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
            ]
        if mode_key == "l2va":
            return [
                "  ### Group 1 ... Group N-1",
                "  integrated_multimodal_description: [Shot 1] ...   (no alignment line: a plausible earlier state that leads into the next group)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
                "",
                "  ### Group N",
                "  How the reference pictures align with the target video — <Picture 1> (from [Shot M]) aligns with the " + d + ".00-second mark of the target video.",
                "",
                "  integrated_multimodal_description: ...   (the last shot lands exactly on <Picture 1>)",
                "  overall_soundscape: ...",
                "  non_diegetic_music: ...",
            ]
        return [
            "  ### Group k",
            "  integrated_multimodal_description: [Shot 1] ...",
            "  [Shot 2] At 00:02.700, ...   (example time — replace it; or delete this line and keep one shot with no cut at all)",
            "  overall_soundscape: ...",
            "  non_diegetic_music: ...",
        ]

    def _build_multisegment_rule(self, mode_key, group_count, duration_seconds):
        """多段剧情拼接 (>=2) 的专用 system prompt 增强段。

        每组 = 一段独立 10s 视频的完整提示词; 第 k+1 组承接第 k 组结尾并推进新剧情。
        字段结构按模式区分 (Ref2VA 六段 / 基础模式三段 / 关键帧模式对齐行), 并强制每组
        重写 subject_definitions 等全部字段, 不允许「与 Group N 相同」之类的缩写。
        使用「列表 + _NL.join」构造, 避免源码中出现易被工具误转义的反斜杠转义序列。
        """
        n = str(group_count)
        d = str(duration_seconds)
        L = []
        L.append("# Multi-group long video (" + n + " groups, " + d + " seconds each)")
        L.append("The user message contains " + n + " story beats, each prefixed with a marker '[Group k]'; the marker "
                 "tells you which block must dramatize that beat (it is not text to copy). Beats are separated by a "
                 "lone line of '---'. Generate exactly " + n + " COMPLETE prompt blocks, headed "
                 "'### Group 1' ... '### Group " + n + "', in order.")
        L.append("Each block is a self-contained prompt for ONE " + d + "-second clip. The clips are concatenated in order "
                 "into one long film, so Group k+1 continues exactly where Group k stopped and then moves the story forward.")
        L.append("")
        L.append("## Required structure of EVERY block")
        L.extend(self._group_skeleton(mode_key, d))
        L.append("")
        L.append("## Continuity rules (MANDATORY)")
        rules = [
            "HAND-OFF: Group k+1 opens on the exact final visible state of Group k — same pose, same room, same light, "
            "same props, same clothing state — in ONE clause (e.g. 'the door Group 1 left open still swings'), then starts "
            "its own new action in the same breath.",
            "NO REPLAY: never re-describe or re-shoot what an earlier group already showed, and never repeat a line of "
            "dialogue. If Group 1 showed him slamming the money down, Group 2 does not slam it down again.",
            "LINE OWNERSHIP: a supplied line belongs to the beat that contains it. Group k speaks its own beat's lines "
            "(plus lines you invent to bridge inside that beat); never move a later beat's line into an earlier group, "
            "and never repeat the same line in two groups.",
            "ALWAYS ADVANCE: every group adds at least one new action, reaction or piece of information. If a beat would "
            "repeat the previous group, escalate it or show its consequence instead.",
            "CORRECT INDEX: each summary names the group it continues from, and the index is correct — Group 2 "
            "continues Group 1, Group 3 continues Group 2, and so on. Never say Group 3 continues Group 1.",
            "ONE BEAT = ONE GROUP: exactly " + n + " groups for " + n + " beats. Never split one beat into two groups "
            "and never merge two beats into one group, however many actions a beat contains — design 1–4 shots "
            "inside that group.",
            "BEAT BOUNDARIES: Group k dramatizes beat k only. No action or line that belongs to beat k+1 may happen "
            "inside Group k — no early exit, no early arrival, no early confession — and every supplied action and "
            "line of beat k must happen inside Group k.",
            "SHOTS AND CUTS: design the shot count (1–4) and each shot's length from the beat itself. A group with one "
            "action may be a single take — a long take that follows the action, or a locked-off static view — but a "
            "beat with several actions, a viewpoint change, or a reaction worth seeing usually needs 2–3 shots. Across "
            "the " + n + " groups, mix single takes and cuts: do not give every group the same shot pattern. Every cut "
            "must be justified by what happens at that moment — put the cut time where the previous action ends, never "
            "copy a template time and never reuse another group's cut list.",
            "CARRY THE LOOK: every group restates every subject in full as it looks at the START of that group, including "
            "accumulated changes (jacket off stays off, blood on the collar stays, hair soaked by the rain stays soaked).",
            "NO ABBREVIATION: every block repeats the complete structure with real content. Never write 'same as Group 1', "
            "'continues as before', 'omitted', '...' or leave a field empty. A subject that appears in more than one "
            "group keeps its number in every group (<Subject 1> is always the same person); a subject local to one "
            "group takes the next unused number in that group. Speaker IDs also keep their numbers (S1 stays S1).",
            "REFERENCE EVERY GROUP: every group's subject_definitions names the reference asset for each subject that "
            "has one — '<Subject 1> is the woman in <Picture 1>, ...' — and rewrites how that subject looks at the "
            "start of the group. Never drop the <Picture N> citation after Group 1 and never reduce a subject to "
            "'the same woman'. With no attached image, rewrite the full text description (build, hair, face, "
            "clothing state) in every group and invent no <Picture N>.",
            "TIMESTAMPS restart inside every group: [Shot 1] has no timestamp; later cuts are strictly increasing within "
            "0.00–" + d + ".00 s and the group's last shot ends before " + d + ".00 s. A single-shot group has no cut "
            "time at all.",
            "CONSISTENCY: same Visual style and Background music style in every group (unless the user asks for a change), "
            "same location, same time of day, same cast. Do not re-cast or re-design anyone.",
            "OUTPUT: all " + n + " blocks in one reply, nothing before '### Group 1', nothing after the last "
            "non_diegetic_music line.",
        ]
        for i, rule in enumerate(rules, 1):
            L.append(str(i) + ". " + rule)
        L.append("")
        L.append("FINAL CHECK before you answer: every group must contain every field of this mode — a group that stops "
                 "after detailed_description is incomplete. count your '### Group' headers — there must be exactly " + n + ", never an "
                 "extra group for a busy beat (compress its actions into 1–4 shots instead). Then list every group's "
                 "shot count and cut times. If two groups share the same cut time, or a cut sits at the exact midpoint "
                 "of the clip with no action landing there, move it to the moment that group's action actually ends. A "
                 "group with one continuous shot has no cut time — that is allowed, and it is better than a midpoint "
                 "cut. Every shot is its own line starting with `[Shot N]`; 'the camera cuts to' buried inside a "
                 "paragraph is a format error. If a beat says someone shouts, calls, answers or curses, those words "
                 "must be inside <d>.")
        L.append("")
        L.append("Follow every rule from the mode-specific system prompt (field names, section order, Chinese dialogue, "
                 "camera vocabulary, quality checks) inside each block.")
        return _NL.join(L)

    # ---------- API 调用 (可选: 直接产出 H3 提示词) ----------

    @staticmethod
    def _norm_api_url(api_url):
        """规范化 API 地址: 去空白/斜杠, 自动补 http:// 前缀 (否则 urllib 报 unknown url type)。"""
        url = (api_url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "http://" + url
        return url

    @staticmethod
    def _is_private_host(host):
        """判断目标主机是否为私网/回环地址。这类地址必须直连, 不能走 HTTP 代理
        (否则环境变量 HTTP_PROXY 指向的代理没开时, urllib 会报 WinError 10061)。"""
        if not host:
            return False
        low = host.lower()
        if low in ("localhost",):
            return True
        try:
            import socket
            ip = socket.gethostbyname(low)
        except Exception:
            return False
        if ip.startswith("127.") or ip == "::1":
            return True
        if ip.startswith("10.") or ip.startswith("192.168."):
            return True
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return True
            except Exception:
                pass
        return False

    @staticmethod
    def _build_opener(url):
        """按目标地址选择 opener: 私网/回环地址 → 无代理直连; 公网地址 → 跟随环境变量代理。"""
        host = urlparse(url).hostname or ""
        if H3PromptGenerator._is_private_host(host):
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    @staticmethod
    def _open_url(req, url, timeout):
        """打开请求。若公网代理指向的本机端口没开 (WinError 10061/连接被拒), 自动改直连重试一次。"""
        opener = H3PromptGenerator._build_opener(url)
        try:
            return opener.open(req, timeout=timeout)
        except urllib.error.URLError as e:
            msg = (str(getattr(e, "reason", "")) + " " + str(e)).lower()
            if "10061" in msg or "refused" in msg or "积极拒绝" in msg:
                # 注意: 失败的代理请求会污染原 Request (host 被改成代理地址),
                # 直连必须用全新 Request, 否则会继续连代理端口。
                headers = {k: v for k, v in req.headers.items() if k.lower() != "host"}
                direct_req = urllib.request.Request(
                    req.full_url, data=req.data, headers=headers, method=req.get_method())
                direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                return direct.open(direct_req, timeout=timeout)
            raise

    @staticmethod
    def _models_url(api_url):
        base = H3PromptGenerator._norm_api_url(api_url)
        return base + "/models" if base.endswith("/v1") else base + "/v1/models"

    @staticmethod
    def _fetch_models(api_url, api_key=""):
        """GET /v1/models; 返回 (模型 id 列表, 耗时秒)。用于“测试连接 / 拉取模型”。"""
        url = H3PromptGenerator._models_url(api_url)
        headers = {"Accept": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = "Bearer " + api_key.strip()
        req = urllib.request.Request(url, headers=headers, method="GET")
        t0 = time.time()
        with H3PromptGenerator._open_url(req, url, 20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = [m.get("id") or m.get("name") for m in (body.get("data") or body.get("models") or [])]
        return [m for m in models if m], time.time() - t0

    @staticmethod
    def _api_completions_url(api_url):
        """把用户填的地址规整成 /chat/completions 端点。

        支持两种写法:
          http://host:port/v1            → http://host:port/v1/chat/completions
          http://host:port                → http://host:port/v1/chat/completions
        """
        base = H3PromptGenerator._norm_api_url(api_url)
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    @staticmethod
    def _images_to_data_urls(image):
        """把 ComfyUI IMAGE (torch.Tensor [B,H,W,C] / numpy / PIL 列表) 转成 data URL 列表。

        返回 ["data:image/png;base64,...", ...]; 任何异常返回 [] (调用方自行兜底)。
        """
        import base64
        import io

        try:
            from PIL import Image as PILImage
        except Exception:
            return []
        import numpy as np

        def _to_np(img):
            # torch.Tensor → numpy (兼容 CUDA: 先 .detach().cpu())
            if hasattr(img, "detach") and hasattr(img, "cpu"):
                img = img.detach().cpu()
            if hasattr(img, "numpy"):
                return np.asarray(img.numpy())
            return np.asarray(img)

        def _pil_of(img):
            arr = _to_np(img)
            if arr.ndim == 4:  # [B,H,W,C] 取第一张 (batch 由外层遍历)
                arr = arr[0]
            if arr.ndim != 3:
                return None
            # float 0-1 → uint8 0-255
            if arr.dtype != np.uint8:
                arr = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)
            # 通道: 只保留前 3 (RGB)
            if arr.shape[-1] >= 3:
                arr = arr[:, :, :3]
            else:
                arr = np.repeat(arr, 3, axis=-1)
            return PILImage.fromarray(arr)

        urls = []
        try:
            if image is None:
                return []
            if isinstance(image, (list, tuple)):
                items = image
            elif hasattr(image, "shape") and getattr(image, "ndim", 0) == 4:
                # [B,H,W,C] → 逐张
                arr = _to_np(image)
                items = [arr[i] for i in range(arr.shape[0])]
            else:
                items = [image]
            for it in items:
                pil = _pil_of(it)
                if pil is None:
                    continue
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                urls.append("data:image/png;base64," + b64)
        except Exception:
            return []
        return urls

    def _call_api(self, api_url, system, user_prompt, image=None, seed=None,
                  temperature=0.9, top_p=0.95, max_tokens=8192, timeout=600,
                  api_key=None, model_name="", top_k=40, enable_thinking=False,
                  reasoning_budget=512):
        """调用 OpenAI 兼容 API 生成提示词 (支持多模态传图 + 固定种子 + 采样参数覆盖 + Bearer Key)。

        返回 (final_prompt, status):
          - 成功: (提示词文本, "OK ...")
          - 失败: ("", 中文原因) —— 绝不抛异常, 由调用方决定如何处理。
        """
        url = self._api_completions_url(api_url)
        # 用户消息: 有图时用 OpenAI 多模态 content 数组 (text + image_url)
        data_urls = self._images_to_data_urls(image) if image is not None else []
        if data_urls:
            content = [{"type": "text", "text": user_prompt or ""}]
            for u in data_urls:
                content.append({"type": "image_url", "image_url": {"url": u}})
        else:
            content = user_prompt or ""
        payload = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # Qwen3 思考模式: 思考走 reasoning_content, 正文在 content;
        # reasoning_budget 限制思考 token 数 (llama.cpp 请求参数, 实测有效)。
        budget = int(reasoning_budget)
        thinking = bool(enable_thinking) and budget != 0
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
        if thinking and budget > 0:
            payload["reasoning_budget"] = budget
        if top_k:
            payload["top_k"] = int(top_k)
        if seed is not None:
            payload["seed"] = int(seed) & 0xFFFFFFFF
        # 本地 llama.cpp / LM Studio 支持 cache_prompt: 复用 system 前缀的 KV 缓存,
        # 同一模式反复生成时省掉整段 prefill (实测 5.3K token 从 ~30s 降到 <1s)。
        # 公网 OpenAI 接口不认识该字段会报 400, 所以只对私网/回环地址发送。
        if self._is_private_host(urlparse(url).hostname or ""):
            payload["cache_prompt"] = True
        if model_name and model_name.strip():
            payload["model"] = model_name.strip()
        headers = {"Content-Type": "application/json"}
        if api_key and api_key.strip():
            headers["Authorization"] = "Bearer " + api_key.strip()
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        # 私网/回环地址直连; 公网若代理未开 (10061) 会自动改直连重试一次
        t0 = time.time()
        try:
            with self._open_url(req, url, timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return "", "API 返回 HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            return "", "无法连接 API: %s" % (e.reason,)
        except Exception as e:
            return "", "API 请求失败: %s" % e
        try:
            choice = body["choices"][0]
            msg = choice["message"]
            text = msg.get("content") or ""
            finish = (choice.get("finish_reason") or "").lower()
            # 关闭思考时: content 空可退回 reasoning_content (部分推理模型只给 reasoning)。
            # 开启思考时不回退, 否则会把思考过程当正文写进 final_prompt。
            if not text.strip() and not thinking:
                text = (msg.get("reasoning_content") or "").strip()
            elapsed = time.time() - t0
            if text.strip():
                timing = body.get("timings") or {}
                pred = timing.get("predicted_n")
                tps = timing.get("predicted_per_second")
                detail = ""
                if pred:
                    detail += ", %d tok" % pred
                if tps:
                    detail += " @%.0f tok/s" % tps
                status = "OK (%.1fs, %d 张图%s)" % (elapsed, len(data_urls), detail)
                if finish == "length":
                    status += " | ⚠ 输出触达 max_tokens 上限，可能被截断 (调大 max_tokens)"
                return text.strip(), status
            if thinking:
                return "", ("思考未在预算内产出正文 (finish=%s, max_tokens=%d, reasoning_budget=%d)；"
                            "请调大 max_tokens 或降低 reasoning_budget" % (
                                finish or "?", max_tokens, budget))
            return "", "API 未返回内容 (%.1fs)" % elapsed
        except Exception as e:
            return "", "API 响应解析失败: %s" % e

    # ---------- 主入口 ----------

    def build(self, mode, group_count=1, user_prompt="", duration_seconds=10,
              视觉风格="不指定 / Unspecified", 音乐风格="禁止音乐 / No Music",
              use_api=False, api_url="", use_random_seed=True, seed=42,
              system_extra="", image=None, api_key=None, model_name="",
              consume_pregen=True, temperature=0.9, top_p=0.95, top_k=40,
              enable_thinking=False, max_tokens=8192, reasoning_budget=512):
        mode_key = _mode_key(mode)
        segments = self._split_segments(user_prompt)
        group_count = int(group_count)
        final_prompt = ""
        api_status = ""
        sampling = dict(api_key=api_key, model_name=model_name,
                        consume_pregen=consume_pregen, mode_key=mode_key,
                        temperature=float(temperature), top_p=float(top_p),
                        top_k=int(top_k), enable_thinking=bool(enable_thinking),
                        max_tokens=int(max_tokens), reasoning_budget=int(reasoning_budget))

        # 单段路径 (group_count == 1): 与原节点行为完全一致, user_prompt 视为单段
        if group_count == 1:
            if not segments:
                prompt = (user_prompt or "").strip()
            else:
                prompt = segments[0]
            if mode_key == "t2va" and not prompt:
                raise ValueError("T2VA 模式必须填写 user_prompt (无参考图可用)。")
            if not prompt and mode_key != "t2va":
                prompt = ("Generate the H3 video prompt based on the provided reference image(s). "
                          "Follow the system prompt exactly.")
            system = self._build_single_system(mode_key, duration_seconds, 视觉风格, 音乐风格, system_extra)
            if mode_key == "ref2va" and (use_api or image is not None):
                system = system.rstrip() + _NL + _NL + self._asset_notice(image)
            final_prompt, api_status = self._maybe_api(use_api, api_url, system, prompt, image,
                                                       use_random_seed, seed,
                                                       groups=1, **sampling)
            return system, prompt, final_prompt, api_status

        # 多段路径 (group_count >= 2)
        if not segments:
            raise ValueError(
                "多段模式 (group_count ≥ 2) 必须在 user_prompt 内用单独一行『---』分隔出 %d 段描述。" % group_count
            )
        # 每段前加 [Group k] 标记: 把「哪段剧情属于哪一组」显式绑定, 避免模型把后一段的动作提前塞进前一组
        # (标记只是给模型的对应关系, system prompt 里会说明不要复制进输出)
        prompt = (_NL + _NL + "---" + _NL + _NL).join(
            "[Group %d] %s" % (i, seg) for i, seg in enumerate(segments, 1))

        # 单段 system prompt 基础 (参数块按模式与组数生成)
        system = self._build_single_system(mode_key, duration_seconds, 视觉风格, 音乐风格,
                                           system_extra, groups=group_count)
        # 追加多段拼接规则 (字段结构按模式区分); 剧情段落只在 user message 里, 不重复塞进 system
        system = system.rstrip() + _NL + _NL + self._build_multisegment_rule(
            mode_key, group_count, duration_seconds)
        if mode_key == "ref2va" and (use_api or image is not None):
            system = system.rstrip() + _NL + _NL + self._asset_notice(image)

        final_prompt, api_status = self._maybe_api(use_api, api_url, system, prompt, image,
                                                   use_random_seed, seed,
                                                   groups=group_count, **sampling)
        return system, prompt, final_prompt, api_status

    def _maybe_api(self, use_api, api_url, system, prompt, image=None,
                   use_random_seed=True, seed=42, temperature=0.9, top_p=0.95,
                   max_tokens=8192, api_key=None, model_name="", consume_pregen=True,
                   mode_key=None, groups=1, top_k=40, enable_thinking=False,
                   reasoning_budget=512):
        """use_api=yes 且 api_url 正确 → 调 API 产出 final_prompt; 否则 final_prompt="" 只提示。

        排队预生成: 先尝试命中预生成缓存 (键 = API 请求完整决定因素), 命中则直接返回,
        不重复调用 LLM; 否则正常调 API。consume_pregen=False 供预生成路由使用
        (只生成不入缓存消费; 生成成功后由路由线程写入缓存)。
        任何异常都不抛, 只返回 (final_prompt, api_status 提示)。
        """
        if not use_api:
            return "", "use_api=no: final_prompt 为空 (请用 vlm 节点生成, 或把 use_api 设为 yes 并填写 api_url)"
        url = self._norm_api_url(api_url)
        if not url:
            return "", "use_api=yes 但 api_url 为空: final_prompt 为空 (请填写 OpenAI 兼容地址, 如 http://localhost:8080/v1)"
        seed_arg = None if use_random_seed else int(seed)
        image_mode = image is not None
        key = _pregen_key(system, prompt, url, temperature, top_p, max_tokens,
                          seed_arg, api_key, model_name, image_mode=image_mode,
                          top_k=top_k, enable_thinking=enable_thinking,
                          reasoning_budget=reasoning_budget)
        # 纯文本与带参考图 (多模态) 请求都参与预生成, 但键分命名空间互不串用
        if consume_pregen:
            hit = _consume_pregen(key)
            if hit is not None:
                return hit[0], "预生成命中 (排队时已生成, %s)" % hit[1]
        final, status = self._call_api(url, system, prompt, image=image, seed=seed_arg,
                                       temperature=temperature, top_p=top_p,
                                       max_tokens=max_tokens, top_k=top_k,
                                       enable_thinking=enable_thinking,
                                       reasoning_budget=reasoning_budget,
                                       api_key=api_key, model_name=model_name)
        # 预生成路由线程: 成功 → 写入缓存供正式执行消费
        # (带图用 replace 语义, 改图重排后新结果立即顶替旧结果)
        if status.startswith("OK") and final:
            if getattr(_PREGEN_STORE, "active", False):
                _append_pregen(key, (final, status), replace=image_mode)
        return final, status

    def _build_single_system(self, mode_key, duration_seconds, 视觉风格, 音乐风格, system_extra, groups=1):
        """system prompt 组装 (模式专项 + 参数块 + 用户追加规则); 多组时参数块说明每组时长。"""
        system = self._load_system_prompt(mode_key)
        system = system.rstrip() + "\n\n" + self._build_param_block(
            duration_seconds, 视觉风格, 音乐风格, mode_key, groups)
        if system_extra and system_extra.strip():
            system = system.rstrip() + "\n\n# 用户追加规则\n" + system_extra.strip()
        return system


class ZImagePromptGenerator(H3PromptGenerator):
    """Z-Image 文生图提示词生成器 — 复用 H3PromptGenerator 的 API 直出/传图/代理绕过逻辑。

    与 H3 节点的区别:
      - system prompt 用 z-image 专用规范 (system_prompts/zimage.txt)
      - 去掉 H3 专用输入: mode / group_count / duration_seconds / 视觉风格 / 音乐风格
      - 保留: user_prompt / use_api / api_url / use_random_seed / seed / system_extra / image
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "user_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "描述要生成的内容 (可简略, AI 会补充细节)。示例:\n"
                                   "可爱的女孩\n"
                                   "美丽的女人, 古风\n"
                                   "一只机械猫, 赛博朋克\n\n"
                                   "(也可留空, 只传参考图做风格转化)",
                }),
                "use_api": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "是否直接调用 API 服务器生成提示词。yes 且 api_url 正确时 final_prompt 直接产出; "
                               "否则 final_prompt 空, api_status 提示原因 (不报错)。",
                }),
                "api_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "OpenAI 兼容 API 地址, 如 http://localhost:8080/v1",
                    "tooltip": "OpenAI 兼容的 /v1 地址 (llama.cpp server 等)。可省略 http:// 前缀; "
                               "私网地址自动无代理直连。",
                }),
                "use_random_seed": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "随机种子: yes=每次结果不同; no=固定使用下方 seed, 结果可复现。",
                }),
                "seed": ("INT", {
                    "default": 42, "min": 0, "max": 4294967295, "step": 1,
                    "tooltip": "固定种子值 (use_random_seed=no 时生效)。",
                }),
            },
            "optional": {
                "system_extra": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "追加到 system prompt 的自定义规则 (可选)",
                }),
                "image": ("IMAGE", {
                    "tooltip": "参考图 (可选)。use_api=yes 时按 OpenAI 多模态格式传给 API 服务器, "
                               "做风格转化/图生图 (保留参考图的主体/构图/风格, 应用 user_prompt 的指令)。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("system_prompt", "user_prompt", "final_prompt", "api_status")
    FUNCTION = "build"
    CATEGORY = "H3/提示词生成"

    def build(self, user_prompt="", use_api=False, api_url="", use_random_seed=True,
              seed=42, system_extra="", image=None, api_key=None, model_name="",
              consume_pregen=True):
        """加载 z-image system prompt, 拼装 user_prompt, 可选 API 直出。

        创造性任务用高采样参数 (temperature 0.9 / top_p 0.95) 保证随机性与多样性;
        H3 节点保持低温 (0.1/0.3) 保格式。
        """
        prompt = (user_prompt or "").strip()
        system = self._load_system_prompt("zimage")
        if system_extra and system_extra.strip():
            system = system.rstrip() + _NL + _NL + "# 用户追加规则" + _NL + system_extra.strip()
        # 有图 + 无文字 → 默认占位指令 (风格转化模式)
        if not prompt and image is not None:
            prompt = ("Transform or restyle the provided reference image according to the user's "
                      "intent. Output a complete standalone Z-Image prompt describing the result.")
        final_prompt, api_status = self._maybe_api(use_api, api_url, system, prompt, image,
                                                   use_random_seed, seed,
                                                   temperature=0.9, top_p=0.95,
                                                   max_tokens=2048,
                                                   api_key=api_key, model_name=model_name,
                                                   consume_pregen=consume_pregen)
        return system, prompt, final_prompt, api_status


class Krea2PromptGenerator(H3PromptGenerator):
    """Krea-2 文生图提示词助手 — 复用 H3 的 API 直出/传图/代理绕过逻辑。

    与 Z-Image 节点的区别:
      - system prompt 用 Krea-2 专属规范 (system_prompts/krea2.txt)
      - 新增「画面风格」下拉 (32 项, 强化风格描述): 选中后把强化过的英文风格描述
        **前置注入**提示词开头 (Krea-2 前 15 token 注意力最高, 画质锚定前置)
      - 「写实增强」开关: 对摄影写实类风格自动追加已验证有效的写实尾巴
        (realistic facial feature proportions, ...), 风格化/绘画类风格自动跳过
      - use_api=no 时 final_prompt 仍直接产出本地拼装提示词 (风格前置 + 用户内容),
        可直接粘贴进 Krea-2 工作流的 CLIPTextEncode; use_api=yes 时由 API 润色
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "user_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "描述要生成的内容 (可简略, AI 会补充细节)。示例:\n"
                                   "可爱的16岁亚洲女孩, 穿着可爱的服装\n"
                                   "一只机械猫, 站在雨夜街头\n"
                                   "森林中的北欧木屋, 烟囱飘出炊烟\n\n"
                                   "(也可留空, 只传参考图做风格转化)",
                }),
                "画面风格": (_KREA2_STYLES, {
                    "default": "不指定 / Unspecified",
                    "tooltip": "画面风格 (32 项): 选中后该风格的强化英文描述会被前置注入提示词开头, "
                               "再展开用户内容。Krea-2 对前 15 个 token 的注意力最高, "
                               "画质/风格锚定词前置能稳定整张图的风格。",
                }),
                "写实增强": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "对摄影写实类风格自动追加已验证有效的写实尾巴 "
                               "(realistic facial feature proportions / realistic body proportions / "
                               "authentic hair texture / genuine clothing material / real-world environment)。"
                               "风格化/绘画/3D/动漫类风格自动跳过。",
                }),
                "use_api": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "是否调用 API 服务器润色提示词。yes 且 api_url 正确时, final_prompt 由 LLM "
                               "按 Krea-2 规范重写; no 时 final_prompt 为本地拼装提示词 (风格前置 + 用户内容), "
                               "无需 LLM 也能直接用。",
                }),
                "api_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "OpenAI 兼容 API 地址, 如 http://localhost:8080/v1",
                    "tooltip": "OpenAI 兼容的 /v1 地址 (llama.cpp server 等)。可省略 http:// 前缀; "
                               "私网地址自动无代理直连。",
                }),
                "use_random_seed": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "随机种子: yes=每次润色结果不同; no=固定使用下方 seed, 结果可复现。",
                }),
                "seed": ("INT", {
                    "default": 42, "min": 0, "max": 4294967295, "step": 1,
                    "tooltip": "固定种子值 (use_random_seed=no 时生效)。",
                }),
            },
            "optional": {
                "system_extra": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "追加到 system prompt 的自定义规则 (可选)",
                }),
                "image": ("IMAGE", {
                    "tooltip": "参考图 (可选)。use_api=yes 时按 OpenAI 多模态格式传给 API 服务器, "
                               "做风格转化/图生图 (保留参考图的主体/构图/风格, 应用 user_prompt 的指令)。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("system_prompt", "user_prompt", "final_prompt", "api_status")
    FUNCTION = "build"
    CATEGORY = "H3/提示词生成"

    def _compose_local_prompt(self, 画面风格, 写实增强, user_prompt, image):
        """本地拼装提示词: 风格描述前置 + 用户内容 + (写实增强尾巴, 仅摄影写实类)。

        返回 (local_prompt, base_prompt):
          - local_prompt: 最终可直接用的提示词
          - base_prompt:  发送给 API 的原始请求 (风格名 + 用户内容, 让 LLM 自由重写)
        """
        style_key = 画面风格 or "不指定 / Unspecified"
        hint = _KREA2_STYLE_HINTS.get(style_key, "")
        style_en = style_key.split(" / ")[-1]
        content = (user_prompt or "").strip()
        # 有图 + 无文字 → 默认占位指令 (风格转化模式)
        if not content and image is not None:
            content = ("Transform or restyle the provided reference image according to the user's "
                       "intent. Output a complete standalone Krea-2 prompt describing the result.")
        parts = []
        if hint:
            parts.append(hint)
        if content:
            parts.append(content)
        if 写实增强 and style_key in _PHOTOREAL_STYLES:
            parts.append(_REALISM_TAIL)
        local_prompt = ", ".join(p for p in parts if p).strip(" ,")
        # 给 API 的原始请求: 风格名 (带中文) + 用户内容
        base_prompt = local_prompt
        if content and style_en != "Unspecified":
            base_prompt = "Visual style: %s (%s). Content: %s" % (style_key, hint, content)
        elif content:
            base_prompt = content
        return local_prompt, base_prompt

    def build(self, user_prompt="", 画面风格="不指定 / Unspecified", 写实增强=True,
              use_api=False, api_url="", use_random_seed=True, seed=42,
              system_extra="", image=None, api_key=None, model_name="",
              consume_pregen=True):
        """Krea-2 提示词组装: 加载专项 system prompt + 风格指令块, 本地拼装或 API 润色。"""
        local_prompt, base_prompt = self._compose_local_prompt(
            画面风格, 写实增强, user_prompt, image)

        # system prompt: 专项规范 + 选中风格指令块 + 用户追加规则
        system = self._load_system_prompt("krea2")
        style_key = 画面风格 or "不指定 / Unspecified"
        hint = _KREA2_STYLE_HINTS.get(style_key, "")
        if hint:
            style_en = style_key.split(" / ")[-1]
            block = [
                "# Selected visual style (MANDATORY)",
                "- Style: %s" % style_en,
                "- Opening anchors (use verbatim at the very start of the prompt): %s" % hint,
                "- The chosen style governs lighting, color grading, texture, and camera language for the WHOLE image.",
            ]
            system = system.rstrip() + _NL + _NL + _NL.join(block)
        if system_extra and system_extra.strip():
            system = system.rstrip() + _NL + _NL + "# 用户追加规则" + _NL + system_extra.strip()

        if use_api:
            final_prompt, api_status = self._maybe_api(use_api, api_url, system, base_prompt, image,
                                                       use_random_seed, seed,
                                                       temperature=0.85, top_p=0.95,
                                                       max_tokens=2048,
                                                       api_key=api_key, model_name=model_name,
                                                       consume_pregen=consume_pregen)
            return system, (user_prompt or "").strip(), final_prompt, api_status
        # use_api=no: final_prompt 直接产出本地拼装提示词 (无需 LLM, 可立即用于 Krea-2 工作流)
        api_status = ("use_api=no: final_prompt 为本地拼装提示词 (风格描述前置 + 用户内容), "
                      "可直接粘贴进 Krea-2 工作流的 CLIPTextEncode; 设为 yes 并填 api_url 可用 LLM 润色")
        return system, (user_prompt or "").strip(), local_prompt, api_status


_MODE_CHOICES = [
    "MiniMax H3 (视频)",
    "Krea-2 (文生图)",
    "Z-Image (文生图)",
    "自定义 / Custom",
]

_CUSTOM_DEFAULT_SYSTEM = (
    "You are a professional prompt engineering assistant. Rewrite the user's request into ONE "
    "polished, highly detailed generation prompt (image or video). Rules:\n"
    "- Output ONLY the final prompt text, no preamble, no quotes, no translation notes.\n"
    "- Keep it in the same language as the user's request; if the request is in Chinese, write the prompt in Chinese.\n"
    "- Be concrete: subject, appearance, clothing, pose, expression, lighting, background, atmosphere, "
    "camera angle, and style keywords.\n"
    "- 100-200 words; separate the key style description and the subject details with commas."
)


class UnifiedPromptGenerator(Krea2PromptGenerator):
    """统一提示词助手: 通过 mode 下拉在 MiniMax H3 / Krea-2 / Z-Image / 自定义 之间切换。

    - 前端 js/promptgen_mode.js 按 mode 真实重建控件面板 (ComfyWidgets + kjnodes 模式):
      只保留当前模式相关控件, 其余控件从节点上彻底移除 (不是隐藏)。
    - build() 按 mode 分发到对应子节点的实现, 行为与各专用节点完全一致
      (三个专用节点仍保留, 旧工作流不受影响)。
    - 自定义 / Custom 模式: system prompt 由用户直接输入, 只保留提示词注入 / API / seed 控件。
    - api_key / model_name: 所有模式通用, 用于外部 OpenAI 兼容 API (Authorization: Bearer)。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (_MODE_CHOICES, {
                    "default": "Krea-2 (文生图)",
                    "tooltip": "模型模式: MiniMax H3 (视频) / Krea-2 (文生图) / Z-Image (文生图) / 自定义 (Custom)。"
                               "切换后节点面板真实重建, 只显示当前模式相关控件。",
                }),
                "user_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "描述要生成的内容 (可简略, AI 会补充细节)。示例:\n"
                                   "可爱的16岁亚洲女孩, 穿着可爱的服装, 站在樱花树下\n"
                                   "一只机械猫, 站在雨夜街头\n"
                                   "清晨城市街道, 主角推开咖啡馆的门\n\n"
                                   "(H3 有图模式 / Krea-2、Z-Image 也可留空只传参考图)",
                }),
                "use_api": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "是否调用 API 服务器润色/生成提示词。yes 且 api_url 正确时 final_prompt 直接产出; "
                               "no 时: Krea-2 模式本地拼装 (风格+内容, 可直接用), H3/Z-Image/自定义 模式 final_prompt 为空 "
                               "(请接 vlm 节点或开启 API)。",
                }),
                "api_url": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "OpenAI 兼容 API 地址, 如 http://localhost:8080/v1",
                    "tooltip": "OpenAI 兼容的 /v1 地址 (llama.cpp server / OpenAI / 中转站等)。可省略 http:// 前缀; "
                               "私网地址自动无代理直连。",
                }),
                "api_key": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "可选。外部 API 的 Key (OpenAI 等需要鉴权的服务)",
                    "tooltip": "可选。非空时以 Authorization: Bearer <key> 头发送。本地 llama.cpp 不需要可不填。",
                }),
                "model_name": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "可选。模型名 (OpenAI 等需要指定 model; llama.cpp 可不填)",
                    "tooltip": "可选。非空时在请求体中携带 model 字段。本地 llama.cpp 服务可不填。",
                }),
                "use_random_seed": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "随机种子: yes=每次结果不同; no=固定使用下方 seed, 结果可复现。",
                }),
                "seed": ("INT", {
                    "default": 42, "min": 0, "max": 4294967295, "step": 1,
                    "tooltip": "固定种子值 (use_random_seed=no 时生效)。",
                }),
            },
            "optional": {
                # ── H3 专属 ──
                "h3_mode": (MODE_OPTIONS, {
                    "default": "T2VA (文生视频)",
                    "tooltip": "仅 MiniMax H3 模式显示: T2VA(文生视频)/I2VA(首帧)/FL2VA(首尾帧)/"
                               "L2VA(尾帧)/Ref2VA(完整参考)。",
                }),
                "group_count": ("INT", {
                    "default": 1, "min": 1, "max": 99, "step": 1,
                    "tooltip": "仅 H3 模式显示: 剧情组数 / 拼接段数。1=单段; N(≥2)=多段拼接, "
                               "user_prompt 内用单独一行『---』分隔 N 段。",
                }),
                "duration_seconds": ("INT", {
                    "default": 10, "min": 4, "max": 15, "step": 1,
                    "tooltip": "仅 H3 模式显示: 每组视频时长 (秒), MiniMax H3 支持 4–15 秒。",
                }),
                "视觉风格": (_STYLES, {
                    "default": "不指定 / Unspecified",
                    "tooltip": "仅 H3 模式显示 (39 项, 与官方/XB 工作流一致)。",
                }),
                "音乐风格": (_MUSIC, {
                    "default": "禁止音乐 / No Music",
                    "tooltip": "仅 H3 模式显示 (22 项)。「禁止音乐」强制 non_diegetic_music: N/A。",
                }),
                # ── Krea-2 专属 ──
                "画面风格": (_KREA2_STYLES, {
                    "default": "不指定 / Unspecified",
                    "tooltip": "仅 Krea-2 模式显示 (32 项): 风格强化英文描述前置注入提示词开头 "
                               "(Krea-2 前 15 token 注意力最高)。",
                }),
                "写实增强": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "仅 Krea-2 模式显示: 对摄影写实类风格自动追加写实尾巴; 绘画/动漫类自动跳过。",
                }),
                # ── 自定义模式专属 ──
                "custom_system_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "自定义系统提示词 (自定义模式)。留空时使用内置默认提示词工程规则",
                    "tooltip": "仅自定义模式显示: 完全由用户指定的 system prompt。留空则用内置默认规则。",
                }),
                # ── 通用 ──
                "temperature": ("FLOAT", {
                    "default": 0.9, "min": 0.0, "max": 2.0, "step": 0.05,
                    "tooltip": "仅 H3 模式显示: 采样温度。Qwen3.6 常用 0.9。",
                }),
                "top_p": ("FLOAT", {
                    "default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "仅 H3 模式显示: 核采样 top_p。Qwen3.6 常用 0.95。",
                }),
                "top_k": ("INT", {
                    "default": 40, "min": 0, "max": 200, "step": 1,
                    "tooltip": "仅 H3 模式显示: top_k 采样。Qwen3.6 常用 40；0 = 不启用。",
                }),
                "max_tokens": ("INT", {
                    "default": 8192, "min": 256, "max": 32768, "step": 256,
                    "tooltip": "仅 H3 模式显示: 输出 token 上限（不是上下文）。组数多或开思考时调到 12288–16384。",
                }),
                "enable_thinking": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "仅 H3 模式显示: Qwen3 思考模式（开=先推理再输出，质量更稳但慢数倍）。",
                }),
                "reasoning_budget": ("INT", {
                    "default": 512, "min": -1, "max": 8192, "step": 128,
                    "tooltip": "仅 H3 模式显示: 思考 token 预算。-1=不限、0=等同关闭；中等约 512–1024。",
                }),
                "system_extra": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "追加到 system prompt 的自定义规则 (可选)",
                }),
                "image": ("IMAGE", {
                    "tooltip": "参考图 (可选)。use_api=yes 时按 OpenAI 多模态格式传给 API 服务器, "
                               "做风格转化/图生图 (I2VA 首帧 / FL2VA 首尾帧 / L2VA 尾帧 / Ref2VA 多张)。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("system_prompt", "user_prompt", "final_prompt", "api_status")
    FUNCTION = "build"
    CATEGORY = "H3/提示词生成"

    def build(self, mode="Krea-2 (文生图)", user_prompt="", h3_mode="T2VA (文生视频)",
              group_count=1, duration_seconds=10, 视觉风格="不指定 / Unspecified",
              音乐风格="禁止音乐 / No Music", 画面风格="不指定 / Unspecified", 写实增强=True,
              use_api=False, api_url="", api_key=None, model_name="",
              use_random_seed=True, seed=42, custom_system_prompt="",
              system_extra="", image=None, consume_pregen=True,
              temperature=0.9, top_p=0.95, top_k=40, max_tokens=8192,
              enable_thinking=False, reasoning_budget=512):
        """按 mode 分发到子节点实现 (行为与各专用节点完全一致); 自定义模式走 _build_custom。"""
        if mode.startswith("MiniMax"):
            return H3PromptGenerator.build(
                self, h3_mode, group_count=group_count, user_prompt=user_prompt,
                duration_seconds=duration_seconds, 视觉风格=视觉风格, 音乐风格=音乐风格,
                use_api=use_api, api_url=api_url, use_random_seed=use_random_seed,
                seed=seed, system_extra=system_extra, image=image,
                api_key=api_key, model_name=model_name,
                consume_pregen=consume_pregen, temperature=temperature, top_p=top_p,
                top_k=top_k, max_tokens=max_tokens, enable_thinking=enable_thinking,
                reasoning_budget=reasoning_budget)
        if mode.startswith("Krea"):
            return Krea2PromptGenerator.build(
                self, user_prompt=user_prompt, 画面风格=画面风格, 写实增强=写实增强,
                use_api=use_api, api_url=api_url, use_random_seed=use_random_seed,
                seed=seed, system_extra=system_extra, image=image,
                api_key=api_key, model_name=model_name,
                consume_pregen=consume_pregen)
        if mode.startswith("自定义") or mode.startswith("Custom"):
            return self._build_custom(
                user_prompt=user_prompt, custom_system_prompt=custom_system_prompt,
                system_extra=system_extra, use_api=use_api, api_url=api_url,
                api_key=api_key, model_name=model_name,
                use_random_seed=use_random_seed, seed=seed, image=image,
                consume_pregen=consume_pregen)
        # Z-Image
        return ZImagePromptGenerator.build(
            self, user_prompt=user_prompt, use_api=use_api, api_url=api_url,
            use_random_seed=use_random_seed, seed=seed,
            system_extra=system_extra, image=image,
            api_key=api_key, model_name=model_name,
            consume_pregen=consume_pregen)

    def _build_custom(self, user_prompt="", custom_system_prompt="", system_extra="",
                      use_api=False, api_url="", api_key=None, model_name="",
                      use_random_seed=True, seed=42, image=None, consume_pregen=True):
        """自定义模式: system prompt 完全由用户指定 (留空用内置默认), 只做提示词注入 + API。"""
        system = (custom_system_prompt or "").strip()
        if not system:
            system = _CUSTOM_DEFAULT_SYSTEM
        if system_extra and system_extra.strip():
            system = system.rstrip() + _NL + _NL + "# 用户追加规则" + _NL + system_extra.strip()
        prompt = (user_prompt or "").strip()
        if not prompt and image is not None:
            prompt = ("Transform or restyle the provided reference image according to the user's "
                      "intent. Output a complete standalone prompt describing the result.")
        final_prompt, api_status = self._maybe_api(
            use_api, api_url, system, prompt, image, use_random_seed, seed,
            temperature=0.85, top_p=0.95, max_tokens=2048,
            api_key=api_key, model_name=model_name,
            consume_pregen=consume_pregen)
        return system, prompt, final_prompt, api_status


# 只注册综合节点: 三个专用节点 (H3/Z-Image/Krea-2) 已并入 mode 下拉, 不再单独暴露
NODE_CLASS_MAPPINGS = {
    "UnifiedPromptGenerator": UnifiedPromptGenerator,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "UnifiedPromptGenerator": "提示词助手 (MiniMax/Krea-2/Z-Image)",
}

# ─────────────────────────────────────────────────────────────
# 排队预生成 HTTP 路由: POST /h3promptgen/pregen
#   前端在入队时 (钩住 app.queuePrompt) 对每个已启用 API 的提示词助手节点
#   调用本路由 → 后台线程立即调用 LLM 生成提示词 → 写入 _PREGEN_CACHE。
#   任务真正执行到该节点时 _maybe_api 命中缓存直接返回, 不重复调 LLM。
# ─────────────────────────────────────────────────────────────
_PREGEN_NODE_CLASSES = {
    "UnifiedPromptGenerator": UnifiedPromptGenerator,
}

def _filter_inputs(cls, inputs):
    """按节点的 INPUT_TYPES 白名单过滤前端发来的 inputs。

    前端入队预生成时把节点所有 widget 值原样发来, 其中可能包含 ComfyUI 自动追加的
    伴生控件 (如 seed 的 control_after_generate) —— build 签名不认识它们, 直接透传
    会抛 TypeError 导致预生成静默失败。只保留节点声明过的参数。
    """
    known = set()
    for sect in ("required", "optional"):
        known.update(cls.INPUT_TYPES().get(sect, {}))
    return {k: v for k, v in inputs.items() if k in known}


def _run_pregen_thread(node_cls, clean_inputs, count, imgs):
    """预生成后台线程主体 (独立函数便于单测)。"""
    _PREGEN_STORE.active = True
    try:
        node = node_cls()
        for _ in range(count):
            node.build(**clean_inputs, consume_pregen=False, image=imgs)
    finally:
        _PREGEN_STORE.active = False


try:
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/h3promptgen/models")
    async def _models_route(request):
        """body: {api_url, api_key?} → {ok, models, seconds} 或 {ok:false, error}。

        前端“测试连接 / 拉取模型”按钮用; 只做 GET /v1/models, 不产生生成费用。
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        api_url = (data.get("api_url") or "").strip()
        api_key = data.get("api_key") or ""
        if not api_url:
            return web.json_response({"ok": False, "error": "api_url 为空"})
        try:
            models, secs = await asyncio.to_thread(H3PromptGenerator._fetch_models, api_url, api_key)
            return web.json_response({"ok": True, "models": models, "seconds": round(secs, 2)})
        except urllib.error.HTTPError as e:
            return web.json_response({"ok": False, "error": "HTTP %s: %s" % (e.code, e.reason)})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    @PromptServer.instance.routes.post("/h3promptgen/pregen")
    async def _pregen_route(request):
        """body: {node_type, inputs: {控件名: 值, ...}, count?: 批次份数, image_files?: [文件名...]}

        立即返回 {"ok": true}; 实际 LLM 调用在后台线程执行, 不阻塞入队。
        image_files: 参考图来源文件名 (ComfyUI input 目录), 前端从 LoadImage 链解析;
        带图请求走多模态预生成 (键为独立命名空间, 与纯文本不串用)。
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        node_type = data.get("node_type")
        inputs = data.get("inputs") or {}
        count = int(data.get("count") or 1)
        count = max(1, min(count, 8))
        image_files = data.get("image_files") or []
        if isinstance(image_files, str):
            image_files = [image_files]
        cls = _PREGEN_NODE_CLASSES.get(node_type)
        if cls is None:
            return web.json_response({"ok": False, "error": "unknown node_type"}, status=400)
        if not (inputs.get("use_api") and inputs.get("api_url")):
            return web.json_response({"ok": True, "skipped": True})

        def _load_images():
            """从 ComfyUI input 目录加载参考图 → PIL (与 LoadImage 相同的 exif 转正/RGB)。"""
            if not image_files:
                return None  # 纯文本
            try:
                from PIL import Image, ImageOps
                import folder_paths
                input_dir = folder_paths.get_input_directory()
            except Exception:
                return None
            pils = []
            for name in image_files:
                path = os.path.join(input_dir, os.path.basename(str(name)))
                if not os.path.isfile(path):
                    return None  # 任一文件缺失 → 放弃预生成 (执行时仍正常调 API)
                try:
                    with Image.open(path) as im:
                        im = ImageOps.exif_transpose(im).convert("RGB")
                        pils.append(im.copy())
                except Exception:
                    return None
            return pils

        def _run():
            try:
                imgs = _load_images()
                clean = _filter_inputs(cls, inputs)
                clean.pop("image", None)  # 与显式 image=imgs 分开传
                _run_pregen_thread(cls, clean, count, imgs)
            except Exception:
                pass  # 预生成失败不抛给前端, 正式执行时仍会正常调 API

        threading.Thread(target=_run, daemon=True).start()
        return web.json_response({"ok": True, "started": True})
except Exception as _route_err:
    # 非 ComfyUI 运行环境 (独立测试) 或路由注册失败: 跳过, 节点本身仍可用
    import sys as _sys
    _sys.stderr.write("[comfyui-h3-prompt-gen] 提示: 预生成路由注册失败 (不影响节点本体): %r\n" % (_route_err,))
