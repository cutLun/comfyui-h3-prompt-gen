# comfyui-h3-prompt-gen

ComfyUI 自定义节点：**提示词助手（MiniMax H3 / Krea-2 / Z-Image / 自定义）** —— 纯提示词工程节点，一个节点覆盖文生视频、文生图、图生视频等全部提示词场景。自动加载对应模式的专项 system prompt，注入参数块，输出 `system_prompt` + `user_prompt`。可选用 `use_api` 直出 `final_prompt`（接 llama.cpp / OpenAI 兼容服务，支持带参考图多模态）。

**节点本身不调用任何 LLM**（除非开启 `use_api`），推理交给 ComfyUI-llama-cpp_vlm 插件（`llama_cpp_instruct_adv` 节点）或你的 API 服务器——模型加载 / 显存占用由 vlm 插件 + ComfyUI 管理。

只有一个节点：**提示词助手 (MiniMax/Krea-2/Z-Image)**——通过 `mode` 下拉在四个模式之间切换，**面板真实重建**（切换后只保留当前模式的控件，效果与 MiniMaxH3Director 一致）：
- **MiniMax H3 (视频)**：H3 视频提示词（T2VA/I2VA/FL2VA/L2VA/Ref2VA + 多段剧情拼接生成长视频）
- **Krea-2 (文生图)**：32 项画面风格下拉，风格描述强化前置，适配 Krea-2 Turbo 工作流
- **Z-Image (文生图)**：AI 创造性重组、随机性、禁腮红
- **自定义 / Custom**：完全自定义系统提示词，只保留提示词注入 / API / seed 控件

## 安装

1. `git clone https://github.com/cutLun/comfyui-h3-prompt-gen` 到 `ComfyUI\custom_nodes\`
2. **重启 ComfyUI**（节点列表 → 右键搜索「提示词助手」）
3. 用 vlm 插件自带节点加载模型：`llama_cpp_model_loader`（选 GGUF 模型）+ `llama_cpp_parameters`（上下文/GPU 层数等），无需外部 llama-server；或直接开 `use_api` 接任意 OpenAI 兼容 API

## 模式：MiniMax H3 (视频)

### 输入

| 参数 | 说明 |
|---|---|
| `mode` | 模式选择：T2VA（文生视频）/ I2VA（首帧参考）/ FL2VA（首尾帧参考）/ L2VA（尾帧参考）/ Ref2VA（完整参考）。每个模式自动加载 `system_prompts/{mode}.txt` 专项 system prompt |
| `group_count` | **剧情组数 / 拼接段数**（新增）。设为 `1` = 单段（与原节点完全一致）。设为 `N (≥2)` 时进入**多段拼接模式**：在 `user_prompt` 内用单独一行 `---` 分隔出 N 段剧情描述，本节点生成一个专门 system prompt，指令 vlm 节点产出 **N 组完整 H3 提示词**，每组输出为 `### Group k` + 标准 H3 三字段；且**第 i+1 组画面/情节必须衔接第 i 组结尾**（同一角色/场景/视觉风格连续、承接上一组末帧动作、保留未解决的悬念钩子）。以此类推可生成无限组，N 组依次拼接即构成一条长视频。
| `user_prompt` | **主提示词**：场景 / 角色 / 动作 / 台词。T2VA 必填；有图模式（I2VA 等）可留空，只把参考图接给 vlm 节点的 `images` 输入 |
| `duration_seconds` | 视频时长（秒，H3 支持 4–15），注入 system prompt 的 Target Video Parameters 块 |
| `视觉风格` | 下拉选择（39 项，与官方/XB 工作流一致：电影感/实拍/复古胶片/黑白电影/纪录片/极简广告/微距/航拍/二维动画/三维CG/日系二次元/美式漫画/皮克斯3D/定格动画/手绘发光/像素艺术/赛博朋克/蒸汽朋克/故障艺术/羊毛毡/折纸/水彩/粘土动画/水墨/油画/纸艺拼贴/剪纸/铅笔素描/浮世绘/敦煌壁画/青花瓷/工笔画/皮影戏/中国风插画/年画/布艺/蜡笔画/哥特萝莉）。选中后强制 `[Shot 1]` 以英文风格名开头并展开具体视觉描述；"GLOBAL MATERIAL OVERRIDE" 风格（水墨/粘土/羊毛毡等）会整体替换世界材质 |
| `音乐风格` | 下拉选择（22 项：禁止音乐/不指定/钢琴/管弦乐/原声吉他/电子/氛围/合成器浪潮/芯片音乐/Lo-fi/史诗/悬疑/浪漫弦乐/摇滚/爵士/嘻哈/放克/纯人声合唱/极简拟音/国风民乐/戏曲/古琴）。「禁止音乐」强制 `non_diegetic_music: N/A` |
| `use_api` | **是否直接用 API 生成提示词**（新增）。设为 yes 且 `api_url` 填了正确的 OpenAI 兼容地址时，`final_prompt` 输出口直接产出 H3 提示词；否则 `final_prompt` 输出空字符串，原因通过 `api_status` 输出口给出（中文提示，**不报错**）。 |
| `api_url` | **OpenAI 兼容 API 地址**（新增）。如 `http://localhost:8080/v1`（llama.cpp server）。支持带 `/v1` 或不带两种写法，**可省略 `http://` 前缀（自动补全）**。地址错误/为空时 `final_prompt` 为空、`api_status` 提示原因，不抛异常。 |
| `use_random_seed` | **随机种子开关**（新增）。yes=每次生成结果不同（不传 seed 给 API）；no=固定使用下方 `seed` 值，结果可复现。 |
| `seed` | **固定种子值**（新增，0–4294967295，默认 42）。`use_random_seed=no` 时生效。 |
| `system_extra` | **追加自定义规则**：拼接到 system prompt 尾部（如"整片保持胶片颗粒质感"） |
| `image` | **参考图**（新增，可选，IMAGE 类型）。`use_api=yes` 时按 OpenAI 多模态格式传给 API 服务器：I2VA 1 张（首帧）、FL2VA 2 张（首尾帧，batch 接入）、L2VA 1 张（尾帧）、Ref2VA 多张。`use_api=no` 时本输入无影响（参考图照旧接 vlm 节点的 `images`） |

### 输出

| 输出 | 说明 |
|---|---|
| `system_prompt` | 完整 system prompt（模式专项 + Target Video Parameters 参数块 + 多段拼接规则 + 用户追加规则）→ 接 `llama_cpp_instruct_adv.system_prompt` |
| `user_prompt` | 主提示词原文（有图模式留空时输出占位指令）→ 接 `llama_cpp_instruct_adv.custom_prompt` |
| `final_prompt` | **API 直出提示词**（新增）。`use_api=yes` 且 `api_url` 正确时，直接产出最终 H3 提示词（含 `### Group k` 多段结构）；否则为空字符串 |
| `api_status` | **API 状态提示**（新增）。成功时 `OK (xx.xs)`；未启用/地址为空/地址错误/请求失败时给中文原因（只提示，**不报错**） |

## 典型接线

```
                        ┌─► system_prompt ─┐
H3 提示词生成器 ─────────┤                 ├─► llama_cpp_instruct_adv (preset_prompt = "Empty - Nothing")
   (mode=T2VA, 视觉风格=水墨)                │         ▲
                        └─► user_prompt ───┘         │
                                           llama_cpp_model_loader ──► llama_model
                                           llama_cpp_parameters ────► parameters
```

要点：
- `llama_cpp_instruct_adv` 的 **`preset_prompt` 必须选 "Empty - Nothing"**，否则预设会覆盖 `custom_prompt`
- 有图模式（I2VA/FL2VA/L2VA/Ref2VA）：把 Load Image 同时接到 `llama_cpp_instruct_adv.images` 输入，模型才能"看见"参考图
- 显存紧张时开 `llama_cpp_instruct_adv.force_offload`；`llama_cpp_parameters` 里控制 `gpu_layers`（12GB 卡 9B 模型建议全层或近全层）
- 节点输出接好后：`llama_cpp_instruct_adv` 的输出（响应文本）→ 你的 H3 工作流 prompt 输入

## 模式：Z-Image (文生图)

Z-Image 是阿里通义 6B 单流 DiT 文生图模型（Turbo 版 8 步蒸馏、无 CFG、写实+美学+中英文字渲染强）。本节点复用 H3 节点的 API 直出/传图/代理绕过逻辑，但 system prompt 换成 **Z-Image 专属规范**（`system_prompts/zimage.txt`）。

### 输入

| 参数 | 说明 |
|---|---|
| `user_prompt` | **主提示词**：可非常简略（如"可爱的女孩"），AI 会补充细节。也可留空只传参考图做风格转化 |
| `use_api` / `api_url` / `use_random_seed` / `seed` | 同 H3 节点（API 直出 / 固定种子） |
| `system_extra` | 追加自定义规则（可选） |
| `image` | **参考图**（可选，IMAGE 类型）。`use_api=yes` 时按 OpenAI 多模态格式传给 API，做风格转化/图生图（保留参考图主体/构图/风格，应用 user_prompt 指令） |

### 内置规则（system prompt 已硬性约束 AI）

1. **规范重组**：把用户普通提示词重组成 Z-Image 规范的完整描述（主体 → 发型 → 妆容 → 服装 → 配饰 → 姿态 → 环境 → 光线 → 摄影风格），输出自然描述句（非 tag 堆叠），纯英文 + 保留用户关键概念。
2. **创造性具象化**：把抽象形容词变成具体设计——如"可爱的女孩"必须落到：发型（双马尾/波波头）、发饰（草莓发夹/缎带）、服装（海军领连衣裙+蕾丝/围裙+印花）、鞋子（玛丽珍鞋+心形扣）、整体轮廓/配色/小挂饰。**不能死板地重复泛词**。
3. **随机性**：泛用输入（如"美丽的女人"）每次给出**不同的具体设计**——古风/旗袍/和服/都市丽人/cosplay/女仆装/日式偶像/哥特/街头风等轮换（可改良设计，不必严格还原），身高/身材/发型/发色/妆容/服装细节明确。API 调用用高采样参数（temperature 0.9 / top_p 0.95）保证多样性（H3 保持 0.1/0.3 保格式）。
4. **禁腮红**：绝对禁止 blush/flush/rosy/shy/embarrassed 及任何提及脸颊变红的概念（连"rather than blushing"这类否定式也不允许——模型仍会看到禁词），用 soft smile / bright eyes / confident gaze 等安全表情描述。**实测：Z-Image 遇到腮红类词汇会生成奇怪的脸。**
5. **只输出提示词**：无前言、无解释、无格式标记。

### 典型接线（API 直出）

```
Load Image ──► image ─┐
                       ├─► Z-Image 提示词生成器 ──► final_prompt ──► Z-Image 出图节点 prompt
"可爱的女孩" ──► user_prompt ─┘   (use_api=yes, api_url=笔记本服务)
```

或接 vlm 节点：`system_prompt`/`user_prompt` → `llama_cpp_instruct_adv`，`preset_prompt` 选 "Empty - Nothing"。

## 模式：Krea-2 (文生图)

Krea-2 是 Comfy-Org 开源的 12B DiT 文生图模型（Turbo 版 8 步蒸馏、无 CFG，文本编码器 Qwen3-VL-4B，中英文皆可）。本节点复用 H3/Z-Image 的 API 直出/传图/代理绕过逻辑，但 system prompt 换成 **Krea-2 专属规范**（`system_prompts/krea2.txt`），并新增 **画面风格下拉**。

### 输入

| 参数 | 说明 |
|---|---|
| `user_prompt` | **主提示词**：描述要生成的内容（可简略，AI 会补充细节）。也可留空只传参考图做风格转化 |
| `画面风格` | **下拉选择（32 项）**：极致写实摄影/电影感/商业广告/复古胶片/黑白艺术/微距摄影/航拍/森系清新/小红书网红/抖音网红/美颜写真/三维渲染/皮克斯3D/低多边形/日系二次元/美式漫画/国漫插画/像素艺术/手绘插画/水彩/水墨国风/油画/浮世绘/敦煌壁画/剪纸/赛博朋克/蒸汽朋克/梦幻幻想/哥特暗黑/极简主义/玻璃质感。选中后该风格的**强化英文描述被前置注入提示词开头**（Krea-2 对前 15 个 token 注意力最高，画质/风格锚定词前置能稳定整张图风格） |
| `写实增强` | **开关（默认开）**：对摄影写实类风格自动追加已验证有效的写实尾巴（`realistic facial feature proportions / realistic body proportions / authentic hair texture / genuine clothing material / real-world environment`，取自你的 Krea-2 工作流正向提示词）；风格化/绘画/3D/动漫类风格自动跳过 |
| `use_api` / `api_url` / `use_random_seed` / `seed` | 同 Z-Image 节点（API 直出 / 固定种子）。润色用高采样参数（temperature 0.85 / top_p 0.95）保证多样性 |
| `system_extra` | 追加自定义规则（可选） |
| `image` | **参考图**（可选，IMAGE 类型）。`use_api=yes` 时按 OpenAI 多模态格式传给 API，做风格转化/图生图 |

### 输出（4 个，与 Z-Image 一致）

| 输出 | 说明 |
|---|---|
| `system_prompt` | Krea-2 专项规范 + 选中风格指令块 + 用户追加规则 |
| `user_prompt` | 用户内容原文 |
| `final_prompt` | **可直接用的提示词**。`use_api=no` 时 = 本地拼装（风格描述前置 + 用户内容 + 写实尾巴），**无需 LLM 也能直接用**；`use_api=yes` 且 api_url 正确时 = LLM 按 Krea-2 规范润色后的完整提示词 |
| `api_status` | 状态提示（成功 / 未启用 / 地址错误等，只提示不报错） |

### 典型用法（Krea-2 工作流）

1. 工作流里放本节点：选 `画面风格`（如「极致写实摄影」），填 `user_prompt`（如「可爱的16岁亚洲女孩，穿着可爱的服装」），`写实增强` 保持开。
2. `use_api=no`：`final_prompt` 直接给出可粘贴的提示词 → 复制进 `Krea2+TurboV2文生图` 工作流的 CLIPTextEncode 正向提示词即可出图。
3. `use_api=yes` + `api_url`（如 `http://localhost:8080/v1`）：`final_prompt` 由 LLM 润色（更丰富的光影/材质/构图描述）。
4. 也可以把 `final_prompt` 输出直接连到 CLIPTextEncode 的 text 输入（控件转输入）——见随附的 `Krea2+TurboV2文生图-提示词助手.json` 示例工作流。

> 实测（2026-09-05）：本地拼装提示词与 API 润色提示词均已在你机器上的 `Krea2+TurboV2文生图` 工作流（UNETLoader int8 + Qwen3-VL CLIP + realism_engine LoRA，2:3 / 2MP，euler/simple 8 步）真实出图验证通过。

## 四合一总览

一个节点搞定四个模式：`mode` 下拉切换 **MiniMax H3 (视频) / Krea-2 (文生图) / Z-Image (文生图) / 自定义 (Custom)**。切换 mode 时节点**面板真实重建**（`js/promptgen_mode.js` 前端扩展，ComfyWidgets + kjnodes 模式，与 XB_ToolBox 动态控件同机制）：非当前模式的控件**从节点上彻底移除**（不是隐藏），效果与 MiniMaxH3Director 的"换模式换面板"一致。切换时同名通用控件的值（如 `user_prompt`/`api_url`）自动保留，切回时专属控件值也恢复。

> **版本变更**：早期版本的三个专用节点（H3 / Z-Image / Krea-2 提示词生成器）已并入综合节点，**不再单独注册**——节点列表里只有一个「提示词助手 (MiniMax/Krea-2/Z-Image)」。旧工作流若仍引用专用节点类型，加载时会显示为缺失节点，请改用综合节点（`mode` 选择对应模式即可，参数完全一致）。

> **前端扩展加载要求（重要）**：新版 ComfyUI 不再自动加载自定义节点的 `js/` 目录，必须在 `__init__.py` 声明 `WEB_DIRECTORY = "./js"`（与 XB_ToolBox / ComfyUI-Manager 相同机制），否则 `/extensions` 里没有我们的 JS，面板重建与排队预生成都不生效。插件已内置该声明；如果面板重建不生效，请先确认 `E:\...\custom_nodes\comfyui-h3-prompt-gen\__init__.py` 里有 `WEB_DIRECTORY`，并重启 ComfyUI + 浏览器强制刷新（Ctrl+F5）。

### 模式与面板控件

| mode | 专属控件 | 行为 |
|---|---|---|
| MiniMax H3 (视频) | `h3_mode`(T2VA/I2VA/FL2VA/L2VA/Ref2VA)、`group_count`(多段拼接)、`duration_seconds`、`视觉风格`(39项)、`音乐风格`(22项) | 与 H3 提示词生成器完全一致 |
| Krea-2 (文生图) | `画面风格`(32项)、`写实增强` | 与 Krea-2 提示词助手完全一致（`use_api=no` 时 final_prompt 直接本地拼装，无需 LLM） |
| Z-Image (文生图) | 无专属 | 与 Z-Image 提示词生成器完全一致 |
| 自定义 / Custom | `custom_system_prompt`(自定义系统提示词) | 只做提示词注入 + API：system prompt 完全由用户指定（留空用内置默认规则），无风格/音乐等控件 |

通用控件（所有模式都有）：`mode`、`user_prompt`、`use_api`、`api_url`、`api_key`、`model_name`、`use_random_seed`、`seed`、`system_extra`、`image`(端口)。

### 外部 API 鉴权（新增）

`api_url` + `api_key` + `model_name` 三件套支持任意 OpenAI 兼容外部服务：
- `api_key` 非空时，请求自动带 `Authorization: Bearer <key>` 头（OpenAI / 中转站等需要鉴权的服务）；
- `model_name` 非空时，请求体携带 `model` 字段（OpenAI 必需；本地 llama.cpp 可不填）；
- 本地局域网 llama.cpp（如 `http://localhost:8080/v1`）两个都可留空。

### 接线与用法

与各专用节点完全相同：`system_prompt`/`user_prompt` 接 vlm 节点（`llama_cpp_instruct_adv`，`preset_prompt`="Empty - Nothing"），或 `use_api=yes` 让 `final_prompt` 直出。文生图场景（Krea-2/Z-Image）可把 `final_prompt` 直接连到出图工作流的正向提示词。

> 实测（2026-09-08）：统一节点四模式分发与各专用节点输出**逐字节一致**（单元测试全过）；面板重建全流程（新建/加载/切换/值保留）34 项 mock 测试全过；`_call_api` 的 Bearer 头与 model 字段经本地 mock HTTP 服务器捕获验证；Krea-2 模式与自定义模式经局域网 API（localhost:8080）实测润色出高质量提示词（7.0s / 6.0s）。

## 排队预生成（入队即生成，不等执行）

**解决的问题**：ComfyUI 的任务队列是串行执行的——前面有任务在跑时，后面的任务要等它跑完才轮到；而提示词助手的 LLM 调用（约 4–7s）发生在"轮到该任务执行时"，白白加长关键路径。

**现在的行为**：点击 **Queue / Queue Front / Run** 入队时，前端立即把图中所有启用了 `use_api` 的提示词助手节点（四个节点都支持）的参数发给后端路由 `POST /h3promptgen/pregen`；后端在**后台线程**里立刻调 LLM 生成提示词并写入缓存（入队不受阻塞，LLM 与前面正在跑的任务并行）。等任务真正执行到该节点时，从缓存命中**秒回**，不再重复调 LLM——LLM 耗时被移出执行链，缩短总排队时间。

要点：
- **缓存键**：由 system prompt / user_prompt / api_url / 采样参数 / seed / api_key / model_name 的 SHA-256 决定（预生成与正式执行走**同一套 build 代码**，键必然一致）。
- **批量**：`batchCount > 1` 时按份数预生成 N 条，FIFO 依次消费，每次执行取走一条。
- **带参考图（多模态）也预生成**：前端顺着 `image` 连线解析上游——若参考图来自 **Load Image**（或 ImageBatch 合并的多个 Load Image），入队时把文件名发给后端，后端从 ComfyUI `input` 目录加载图片做**多模态预生成**（实测 35B 视觉模型 23.5s 的调用移出执行链，命中 0.000s）。图文使用**独立缓存命名空间**互不串用；改图重排后新结果**替换**旧结果立即生效。
  - **局限性**：若参考图来自**上游生成的图**（VAE Encode / 采样器等输出），入队时该图还不存在，无法预生成——这类节点仍按原逻辑在轮到执行时调 API（约 1 分钟，属正常）；如需预生成收益，请让参考图走 Load Image。
- **自动跳过**：`use_api=no`、`api_url` 为空、或参考图来源无法解析（上游生成图）的节点不预生成。
- **失败降级**：预生成失败（LLM 不可达、图片缺失等）静默丢弃，正式执行时仍按原逻辑正常调 API——行为与旧版一致。
- **缓存上限**：每键最多 32 条、全局最多 512 个键（FIFO 淘汰），长时间运行不膨胀。
- **注意**：改完代码后需**重启 ComfyUI** 才会加载新路由/前端扩展。
- **预期收益说明**：预生成只对**排队中**的任务有效（入队时前面已有任务在跑，LLM 并行进行）；**第一个任务**入队即执行，没有提前量，仍需等 LLM 调用（约 1 分钟属正常）。想给第一个任务也提速，可先点 Queue 排一个长任务，再把提示词任务排在后面。

## 多段剧情拼接（生成长视频）

用户截图里的「多段剧情拼接」需求（参考图片）：用户输入多段描述，Llama-cpp_vlm 输出多组相互衔接的 MiniMax H3 提示词，第 2 组衔接第 1 组尾巴，可无限递增。

### 操作方式
1. 把 `group_count` 设为段数 `N (≥2)`。
2. 在 `user_prompt` 文本框里，每段剧情描述独占一段，用**单独一行 `---`** 分隔。示例：
   ```
   清晨城市街道，主角推开咖啡馆的门。
   ---
   主角走进厨房，发现桌上放着一封未拆的信。
   ---
   主角拆开信件，脸色骤变，冲出门外。
   ```
3. 节点会在 system prompt 里追加 `# Multi-Sequence Continuation` 规则，指令 vlm 节点产出 `### Group 1 / Group 2 / Group 3 ...` 共 N 组**完整** H3 提示词，每组含 `integrated_multimodal_description / overall_soundscape / non_diegetic_music`。
4. vlm 节点（`llama_cpp_instruct_adv`）输出这 N 组后，按顺序分别喂给 N 个 H3 出图/出视频节点，即拼接成一条 N 段连续长视频。

### 衔接保证（系统提示词已硬性约束 vlm）
- **尾帧衔接**：第 i+1 组的第一个镜头必须接上第 i 组结尾（同一姿态/地点/光线/画面道具/机位）。
- **身份一致**：角色、服装、名称、视觉风格跨组保持不变。
- **钩子延续**：上一组结尾的悬念在下一组开头被承接或升级。
- **风格/音乐一致**：各组沿用相同的 `视觉风格` / `音乐风格`（除非用户分指定）。
- **时间戳**：每组内部 `[Shot 1]` 不带时间戳，后续镜头在该组的 `duration_seconds` 内严格递增。

> 注：真正的 N 组生成由 Llama-cpp_vlm 完成；本节点只负责把「多段拼接规则」注入 system prompt。多段模式若 `user_prompt` 为空（无 `---` 分隔），会抛 `ValueError` 提示。

## API 直出模式（无需 vlm 节点）

节点新增 `use_api` 开关 + `api_url` 输入 + `final_prompt` / `api_status` 两个输出口：

1. **`use_api` 设为 yes + 填对 `api_url`** → `final_prompt` 输出口**直接产出**最终 H3 提示词（单段一个 prompt；多段为 `### Group k` 结构）。适合把本节点当纯提示词 AI 助手用（如连你笔记本上的 llama.cpp：`http://localhost:8080/v1`），无需再接 `llama_cpp_instruct_adv`。
2. **`use_api` 为 no** → `final_prompt` 为空，`api_status` 提示 "use_api=no..."，行为与旧版一致（输出 system_prompt + user_prompt 给 vlm 节点）。
3. **`use_api=yes` 但地址为空/错误/不可达** → `final_prompt` 为空，`api_status` 给出中文原因（"api_url 为空" / "无法连接 API: ..." / "API 返回 HTTP 4xx"），**只提示不报错**。

实现细节：
- 自动把 `api_url` 规整成 `/v1/chat/completions`（带不带 `/v1` 后缀都行）。
- **局域网/私网地址自动无代理直连**（`192.168.x` / `10.x` / `172.16-31.x` / `localhost`）：⚠️ 若系统环境变量 `HTTP_PROXY`/`HTTPS_PROXY` 指向的代理（如 Clash `127.0.0.1:7890`）未开启，urllib 走代理会报 `WinError 10061 连接被拒绝`——本节点对私网地址强制绕过代理（实测修复）。公网 API 地址仍跟随环境变量代理。
- 请求带 `chat_template_kwargs: {"enable_thinking": false}` —— 对 Qwen3 等 llama.cpp 推理模型，**必须关 thinking 才能拿到 content**（否则 content 为空、答案全在 reasoning_content 里，实测确认）。
- **图片支持**：`image` 输入接参考图（I2VA/FL2VA/L2VA/Ref2VA），`use_api=yes` 时自动转 PNG base64 按 OpenAI 多模态格式（`image_url`）传给 API——需要 API 服务端带 mmproj 视觉能力（你笔记本的 llama.cpp 已带）。多张用 batch 接入（FL2VA 首尾帧）。实测：笔记本 3060 服务 28.8s 产出带图理解（`<Picture 1> (from [Shot 1]) is fully referenced.`）的 I2VA H3 提示词。
- 生成超时 600s（35B 多段约 1 分钟级）。
- `final_prompt` 可直接接到你 H3 工作流的 prompt 输入（多段时按 `### Group k` 拆分喂给各段出视频节点）。

## 自定义

- **改某模式的规则**：直接编辑 `system_prompts/{mode}.txt`，重启 ComfyUI 生效
- **重新生成专项文件**：`python generate_system_prompts.py`（从 `h3_prompt_gen_system.txt` 完整版裁剪）
- **风格提示词**：`nodes.py` 顶部 `_STYLE_HINTS` / `_MUSIC_HINTS` 字典，可自行增删下拉项与注入文本

## 测试

节点本体为纯 Python（零第三方依赖），不依赖 LLM / ComfyUI 运行时，可独立单元测试。覆盖：单段兼容 / 多段拼接规则 / API 地址规范化 / 缓存键 / use_api 异常场景（只提示不报错）。

## 常见问题

- **vlm 节点输出为空** → 检查 `preset_prompt` 是否 "Empty - Nothing"；`llama_cpp_model_loader` 是否已选模型
- **模型加载慢** → 12GB 卡上 9B 模型全 GPU 加载约 10 秒，属正常；连续跑多次会命中 ComfyUI 缓存不再重载
- **提示词里风格没生效** → 确认 `[Shot 1]` 开头与所选风格一致（风格块里给了 Correct/Wrong 示例）
