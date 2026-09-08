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
import collections
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

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
                api_key, model_name, image_mode=False):
    """由「API 请求的完整决定因素」生成缓存键 —— 前端入队预生成与节点执行时
    走同一套 build 代码, 组装的 system/prompt 一致, 键必然一致。

    image_mode=True 时用独立命名空间: 带参考图 (多模态) 与纯文本请求即使参数
    相同也互不串用 (同一个节点先纯文本后带图, 或反过来, 都不会命中对方缓存)。
    """
    h = hashlib.sha256()
    h.update(_PREGEN_VERSION.encode("utf-8"))
    h.update(b"\x00img" if image_mode else b"\x00txt")
    for part in (system or "", prompt or "", api_url or "",
                 repr(temperature), repr(top_p), repr(max_tokens),
                 repr(seed_arg), api_key or "", model_name or ""):
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

MINI_FALLBACK = """You are an expert MiniMax H3 video prompt writer. Convert the user's request into a precisely formatted H3 video generation prompt.
Output ONLY the final prompt, in English, starting DIRECTLY with the field labels. Preserve dialogue and lyrics verbatim in their original language inside <d>[LANG] ...</d>.
Use this exact format:
integrated_multimodal_description: [Shot 1] <style>, <opening composition> ...
[Shot 2] At MM:SS.mmm, the camera cuts to ...

overall_soundscape: <ambient + physical sounds, 1-4 sentences>

non_diegetic_music: <instrumentation/tempo/dynamics, 1-3 sentences, or N/A>

Rules: [Shot 1] has no timestamp; later shots use strictly increasing cut times inside the video duration. Camera motion is written as natural English (motion type + amplitude + speed). Speakers use stable IDs (S1), (S2). Never translate user dialogue; keep it verbatim in <d>. Visible on-screen text goes in English double quotes. Use N/A where a section does not apply."""

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
                    "tooltip": "剧情组数 / 拼接段数。设为 1 = 单段(与原节点一致); 设为 N(≥2) 时, "
                               "在 user_prompt 内用『---』分隔出 N 段描述, vlm 将输出 N 组相互衔接的 H3 提示词, "
                               "长视频即由这 N 组依次拼接而成。",
                }),
                "user_prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "placeholder": "多段拼接: 每组一段描述, 用单独一行『---』分隔。示例:\n\n"
                                   "清晨城市街道, 主角推开咖啡馆的门。\n"
                                   "---\n"
                                   "主角走进厨房, 发现桌上放着一封未拆的信。\n"
                                   "---\n"
                                   "主角拆开信件, 脸色骤变, 冲出门外。\n\n"
                                   "(单段模式可只写一段, 无需分隔符; I2VA/FL2VA/L2VA/Ref2VA 可留空只传参考图给 vlm 节点)",
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

    def _build_param_block(self, duration_seconds, style, music):
        """时长 + 视觉风格 + 音乐风格 → 参数块 (英文, 与 XB_ToolBox 注入格式一致)"""
        lines = ["## Target Video Parameters (MUST follow exactly)"]
        lines.append(
            f"- Duration: exactly {duration_seconds} seconds (all shot timestamps MUST fall "
            f"within this range; the final shot must end before the {duration_seconds}-second mark)"
        )
        if "Unspecified" not in style:
            en_name = style.split(" / ")[-1]  # "中文 / English" → English
            hint = _STYLE_HINTS.get(style, "")
            lines.append(f"- Visual style: {en_name} — {hint}")
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

    def _build_multisegment_rule(self, group_count):
        """多段剧情拼接 (>=2) 的专用 system prompt 增强段。

        指令 vlm 节点产出 N 组完整 H3 提示词, 第 i+1 组画面/情节衔接第 i 组结尾。
        返回一段英文规则文本, 拼接到各模式 system prompt 的 Target Video Parameters 之后。
        使用「列表 + _NL.join」构造, 避免源码中出现易被工具误转义的 \\n 转义序列。
        """
        L = []
        L.append("# Multi-Sequence Continuation (N = " + str(group_count) + " groups)")
        L.append("The user provides " + str(group_count) + " separate story beats (one paragraph each, separated by a lone line of '---').")
        L.append("Generate exactly " + str(group_count) + " COMPLETE and SELF-CONTAINED MiniMax H3 prompts, one per story beat, in this exact format:")
        L.append("")
        L.append("  ### Group 1")
        L.append("  integrated_multimodal_description: [Shot 1] ...")
        L.append("  overall_soundscape: ...")
        L.append("  non_diegetic_music: ...")
        L.append("")
        L.append("  ### Group 2")
        L.append("  integrated_multimodal_description: [Shot 1] ...")
        L.append("  overall_soundscape: ...")
        L.append("  non_diegetic_music: ...")
        L.append("")
        L.append("  ... (one ### Group k block for every group, in order) ...")
        L.append("")
        L.append("Each group is an independent H3 clip of duration_seconds long. Concatenate them back-to-back to form ONE continuous long video. The groups MUST chain seamlessly AND MUST NOT repeat each other:")
        L.append("")
        L.append("1. **CONTINUITY, NOT REPLAY (MANDATORY)**: Group i+1 picks up where Group i ended, but it must IMMEDIATELY move FORWARD with new story content. Do NOT re-describe the events that already happened in previous groups, do NOT replay their shots, do NOT restate their dialogue. The opening shot of Group i+1 may only establish the minimal spatial/state link (e.g. 'the door the man just opened swings wide', 'the letter lies open in her hands'), then within ONE sentence the new group must start its OWN new beat — new action, new dialogue, new development. If Group 1 already showed the man walking into the cafe, Group 2 must NOT show him walking into the cafe again.")
        L.append("2. **Each group tells ONLY its own beat**: Group k's content is driven by story beat k. Build its shots from that beat alone. Previous groups' events are BACKGROUND FACTS to reference in one clause at most ('having just opened the letter, she...'), never scenes to re-enact.")
        L.append("3. **Always advance the story**: every group must introduce at least one NEW piece of information, NEW action, or NEW reaction not present in any earlier group. If a new group would say the same thing as the previous one, instead escalate or resolve: show the CONSEQUENCE, not the repeated cause.")
        L.append("4. **Carry the hook**: if a group ends on a suspense/unresolved beat, the next group opens by acknowledging it briefly and then resolving or escalating it with a NEW development.")
        L.append("5. **Keep identities constant**: reuse the exact same character descriptions, clothing, names, and visual style across all groups — never reintroduce or alter them. Reuse stable speaker IDs (S1, S2). Do NOT re-introduce characters as if they were new.")
        L.append("6. **Style consistency**: apply the same Visual style and Background music style to every group (unless the user specifies per-group variation). Each Group's [Shot 1] must still open with the Visual style label as required by the mode rules.")
        L.append("7. **Timestamps restart per group**: within each group, [Shot 1] has NO timestamp and later shots use strictly increasing cut times inside that group's duration_seconds; the last shot of each group ends before the group's duration mark.")
        L.append("8. Follow every rule from the mode-specific system prompt (field names, section order, verbatim dialogue, camera-motion vocabulary, quality checks) INSIDE each group. Do NOT invent new field names or merge groups. Output ALL " + str(group_count) + " groups.")
        L.append("")
        L.append("Story beats provided by the user:")
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
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if H3PromptGenerator._is_private_host(host):
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

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
                  temperature=0.1, top_p=0.3, max_tokens=8192, timeout=600,
                  api_key=None, model_name=""):
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
            # llama.cpp 推理模型 (Qwen3 thinking) 必须关掉思考, 否则 content 为空
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if seed is not None:
            payload["seed"] = int(seed) & 0xFFFFFFFF
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
        # 私网/回环地址 (如局域网 llama-server) 必须无代理直连:
        # 否则环境变量 HTTP_PROXY 指向的代理未开时, urllib 报 WinError 10061。
        opener = self._build_opener(url)
        t0 = time.time()
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return "", "API 返回 HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            return "", "无法连接 API: %s" % (e.reason,)
        except Exception as e:
            return "", "API 请求失败: %s" % e
        try:
            msg = body["choices"][0]["message"]
            text = msg.get("content") or ""
            # 推理模型兜底: content 空时尝试 reasoning_content 提取最终答案
            if not text.strip():
                rc = msg.get("reasoning_content") or ""
                text = rc.strip()
            elapsed = time.time() - t0
            if text.strip():
                return text.strip(), "OK (%.1fs, %d 张图)" % (elapsed, len(data_urls))
            return "", "API 未返回内容 (%.1fs)" % elapsed
        except Exception as e:
            return "", "API 响应解析失败: %s" % e

    # ---------- 主入口 ----------

    def build(self, mode, group_count=1, user_prompt="", duration_seconds=10,
              视觉风格="不指定 / Unspecified", 音乐风格="禁止音乐 / No Music",
              use_api=False, api_url="", use_random_seed=True, seed=42,
              system_extra="", image=None, api_key=None, model_name="",
              consume_pregen=True):
        mode_key = _mode_key(mode)
        segments = self._split_segments(user_prompt)
        group_count = int(group_count)
        final_prompt = ""
        api_status = ""

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
            final_prompt, api_status = self._maybe_api(use_api, api_url, system, prompt, image,
                                                       use_random_seed, seed,
                                                       api_key=api_key, model_name=model_name,
                                                       consume_pregen=consume_pregen)
            return system, prompt, final_prompt, api_status

        # 多段路径 (group_count >= 2)
        if not segments:
            raise ValueError(
                "多段模式 (group_count ≥ 2) 必须在 user_prompt 内用单独一行『---』分隔出 %d 段描述。" % group_count
            )
        # 用户段落按原样拼接: 保留 --- 分隔符, 与 system prompt 中 "separated by a lone line of '---'" 一致
        prompt = (_NL + _NL + "---" + _NL + _NL).join(segments)

        # 单段 system prompt 基础
        system = self._build_single_system(mode_key, duration_seconds, 视觉风格, 音乐风格, system_extra)
        # 追加多段拼接规则 + 用户提供的段落
        system = system.rstrip() + _NL + _NL + self._build_multisegment_rule(group_count)
        if prompt:
            system = system + _NL + prompt
        else:
            system = system.rstrip() + _NL + _NL + "(每个段落描述要生成的内容: 场景 / 角色 / 动作 / 台词...)"

        final_prompt, api_status = self._maybe_api(use_api, api_url, system, prompt, image,
                                                   use_random_seed, seed,
                                                   api_key=api_key, model_name=model_name,
                                                   consume_pregen=consume_pregen)
        return system, prompt, final_prompt, api_status

    def _maybe_api(self, use_api, api_url, system, prompt, image=None,
                   use_random_seed=True, seed=42, temperature=0.1, top_p=0.3,
                   max_tokens=8192, api_key=None, model_name="", consume_pregen=True):
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
                          seed_arg, api_key, model_name, image_mode=image_mode)
        # 纯文本与带参考图 (多模态) 请求都参与预生成, 但键分命名空间互不串用
        if consume_pregen:
            hit = _consume_pregen(key)
            if hit is not None:
                return hit[0], "预生成命中 (排队时已生成, %s)" % hit[1]
        final, status = self._call_api(url, system, prompt, image=image, seed=seed_arg,
                                       temperature=temperature, top_p=top_p,
                                       max_tokens=max_tokens,
                                       api_key=api_key, model_name=model_name)
        # 预生成路由线程: 成功 → 写入缓存供正式执行消费
        # (带图用 replace 语义, 改图重排后新结果立即顶替旧结果)
        if status.startswith("OK") and final:
            if getattr(_PREGEN_STORE, "active", False):
                _append_pregen(key, (final, status), replace=image_mode)
        return final, status

    def _build_single_system(self, mode_key, duration_seconds, 视觉风格, 音乐风格, system_extra):
        """单段 system prompt 组装 (模式专项 + 参数块 + 用户追加规则)。"""
        system = self._load_system_prompt(mode_key)
        system = system.rstrip() + "\n\n" + self._build_param_block(duration_seconds, 视觉风格, 音乐风格)
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
              system_extra="", image=None, consume_pregen=True):
        """按 mode 分发到子节点实现 (行为与各专用节点完全一致); 自定义模式走 _build_custom。"""
        if mode.startswith("MiniMax"):
            return H3PromptGenerator.build(
                self, h3_mode, group_count=group_count, user_prompt=user_prompt,
                duration_seconds=duration_seconds, 视觉风格=视觉风格, 音乐风格=音乐风格,
                use_api=use_api, api_url=api_url, use_random_seed=use_random_seed,
                seed=seed, system_extra=system_extra, image=image,
                api_key=api_key, model_name=model_name,
                consume_pregen=consume_pregen)
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
