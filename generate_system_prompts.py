# -*- coding: utf-8 -*-
"""
从已验证的完整版 H3 system prompt (h3_prompt_gen_system.txt) 生成 5 个模式专项文件。
专项化策略:
  - 通用块(角色/输出契约/质量检查) 全模式保留
  - Base modes 的 A(指令行)/模式路径 按模式过滤; B/C/D 全模式保留 (Ref2VA 的 detailed_description 也依赖镜头/说话人/运镜规则)
  - Ref2VA 六段格式仅 ref2va 模式保留
  - 字面输出模板按模式替换为专属模板 (实测: 3B 激活模型必须有字面模板才不丢字段名)
输出: custom_nodes/comfyui-h3-prompt-gen/system_prompts/{mode}.txt
"""
import os, re, sys

SRC = os.environ.get("H3_SYSTEM_SRC") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not SRC or not os.path.isfile(SRC):
    print("用法: python generate_system_prompts.py <完整版system prompt文本路径>")
    print("  或设置环境变量 H3_SYSTEM_SRC 指向该文件")
    sys.exit(1)
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompts")

with open(SRC, "r", encoding="utf-8") as f:
    full = f.read()

# ---- 按一级标题切块 ----
sections = {}
for m in re.finditer(r"(?m)^# .*$", full):
    start = m.start()
    end = re.search(r"(?m)^# ", full[start + 1:])
    end = start + 1 + end.start() if end else len(full)
    title = m.group(0)
    sections[title] = full[start:end].rstrip()

head = sections["# Step 1 — Detect the input mode"]  # 占位, 下面替换
# 实际角色段 = Step 1 之前的内容
step1_pos = full.find("# Step 1")
role = full[:step1_pos].rstrip()

contract = sections["# Output contract (ALWAYS)"]

base = sections["# Base modes (T2VA / I2VA / FL2VA / L2VA) — three core fields in this order"]
# Base modes 内按二级标题切块
subs = {}
for m in re.finditer(r"(?m)^## .*$", base):
    start = m.start()
    end = re.search(r"(?m)^## ", base[start + 1:])
    end = start + 1 + end.start() if end else len(base)
    subs[m.group(0)] = base[start:end].rstrip()

sec_a = subs["## A. Instruction line (omit for T2VA only)"]
sec_b = subs["## B. integrated_multimodal_description"]
sec_c = subs["## C. overall_soundscape"]
sec_d = subs["## D. non_diegetic_music"]
sec_paths = subs["## Mode-specific development paths"]

ref2va = sections["# Full-Reference Mode (Ref2VA)"]
quality = sections["# Quality checks before output"]

# 模式路径条目过滤
path_items = {
    "i2va": [l for l in sec_paths.splitlines() if l.startswith("- I2VA:")],
    "fl2va": [l for l in sec_paths.splitlines() if l.startswith("- FL2VA:")],
    "l2va": [l for l in sec_paths.splitlines() if l.startswith("- L2VA:")],
}

MODE_LOCK = {
    "t2va": "You are currently operating in T2VA mode: no reference image is available. "
            "Build the complete audiovisual timeline of the target video purely from the user's text.",
    "i2va": "You are currently operating in I2VA mode: <Picture 1> is the actual first frame of the target video at 0.00 seconds. "
            "Describe the style, subjects, composition, and scene shown in the image, then develop the timeline forward from it.",
    "fl2va": "You are currently operating in FL2VA mode: Picture 1 is the first frame and Picture 2 is the last frame of the target video. "
             "Describe the continuous motion path that connects them.",
    "l2va": "You are currently operating in L2VA mode: <Picture 1> is the FINAL frame of the target video. "
            "Infer a plausible earlier state, then describe how the action converges to the reference image.",
    "ref2va": "You are currently operating in full-reference Ref2VA mode: the user supplies images/videos/audio as reference assets. "
              "Produce the six-section rewrite format.",
}

# 专属字面模板
TEMPLATE = {
    "t2va": """# Mandatory output format (reproduce these exact field labels, in this exact order)
Begin your reply DIRECTLY with the prompt text. Never start with any words before the first field label.

T2VA must begin exactly like this:
```
integrated_multimodal_description: [Shot 1] Live-action, cinematic, ... 
[Shot 2] At 00:05.000, the camera cuts to ...

overall_soundscape: ...

non_diegetic_music: ...
```

The labels `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` MUST appear in the output verbatim. Do not omit them, rename them, or merge them into prose.""",
    "i2va": """# Mandatory output format (reproduce these exact field labels, in this exact order)
Begin your reply DIRECTLY with the prompt text. Never start with any words before the first field label.

I2VA must begin with the alignment instruction line, then one blank line, then the three core fields:
```
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, ...

overall_soundscape: ...

non_diegetic_music: ...
```

The labels `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` MUST appear in the output verbatim. Do not omit them, rename them, or merge them into prose.""",
    "fl2va": """# Mandatory output format (reproduce these exact field labels, in this exact order)
Begin your reply DIRECTLY with the prompt text. Never start with any words before the first field label.

FL2VA must begin with the alignment instruction line, then one blank line, then the three core fields:
```
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, ...

overall_soundscape: ...

non_diegetic_music: ...
```

The labels `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` MUST appear in the output verbatim. Do not omit them, rename them, or merge them into prose.""",
    "l2va": """# Mandatory output format (reproduce these exact field labels, in this exact order)
Begin your reply DIRECTLY with the prompt text. Never start with any words before the first field label.

L2VA must begin with the alignment instruction line, then one blank line, then the three core fields:
```
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, ...

overall_soundscape: ...

non_diegetic_music: ...
```

The labels `integrated_multimodal_description:`, `overall_soundscape:`, `non_diegetic_music:` MUST appear in the output verbatim. Do not omit them, rename them, or merge them into prose.""",
    "ref2va": """# Mandatory output format (reproduce these exact field labels, in this exact order)
Begin your reply DIRECTLY with the prompt text. Never start with any words before the first field label.

Ref2VA must contain exactly these six labels in this order:
```
subject_definitions: ...
summary: ...
retention_analysis: ...
detailed_description: ...
overall_soundscape: ...
non_diegetic_music: ...
```

The six labels MUST appear in the output verbatim. Do not omit them, rename them, or merge them into prose.""",
}

def build(mode):
    parts = [role]
    parts.append(MODE_LOCK[mode])
    parts.append(contract)
    base_parts = [sec_b, sec_c, sec_d]
    if mode in ("i2va", "fl2va", "l2va"):
        base_parts.insert(0, sec_a)
        items = path_items[mode]
        if items:
            base_parts.append("## Mode-specific development paths\n" + "\n".join(items))
    parts.extend(base_parts)
    if mode == "ref2va":
        parts.append(ref2va)
    parts.append(TEMPLATE[mode])
    parts.append(quality)
    return "\n\n".join(p for p in parts if p.strip()) + "\n"

os.makedirs(OUT_DIR, exist_ok=True)
for mode in ("t2va", "i2va", "fl2va", "l2va", "ref2va"):
    out = os.path.join(OUT_DIR, mode + ".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(mode))
    size = os.path.getsize(out)
    print(f"{mode}.txt  {size/1024:.1f} KB")
print("OUT_DIR:", OUT_DIR)
