# comfyui-h3-prompt-gen

用Ai做的ComfyUI 自定义节点：**提示词助手（MiniMax H3 / Krea-2 / Z-Image / 自定义）** —— 纯提示词工程节点，一个节点覆盖文生视频、文生图、图生视频等全部提示词场景。自动加载对应模式的专项 system prompt，注入参数块，输出 `system_prompt` + `user_prompt`。可选用 `use_api` 直出 `final_prompt`（接 llama.cpp / OpenAI 兼容服务，支持带参考图多模态）。

**节点本身不调用任何 LLM**（除非开启 `use_api`），推理交给 ComfyUI-llama-cpp_vlm 插件（`llama_cpp_instruct_adv` 节点）或你的 API 服务器——模型加载 / 显存占用由 vlm 插件 + ComfyUI 管理。

只有一个节点：**提示词助手 (MiniMax/Krea-2/Z-Image)**——通过 `mode` 下拉在四个模式之间切换：
- **MiniMax H3 (视频)**：H3 视频提示词（T2VA/I2VA/FL2VA/L2VA/Ref2VA + 多段剧情拼接生成长视频）
- **Krea-2 (文生图)**：32 项画面风格下拉，风格描述强化前置，适配 Krea-2 Turbo 工作流
- **Z-Image (文生图)**：AI 创造性重组、随机性、禁腮红
- **自定义 / Custom**：完全自定义系统提示词，只保留提示词注入 / API / seed 控件

## 安装

 `git clone https://github.com/cutLun/comfyui-h3-prompt-gen` 到 `ComfyUI\custom_nodes\`

## 模式：MiniMax H3 (视频)

### 输入

| 参数 | 说明 |
|---|---|
| `mode` | 模式选择：T2VA（文生视频）/ I2VA（首帧参考）/ FL2VA（首尾帧参考）/ L2VA（尾帧参考）/ Ref2VA（完整参考）。每个模式自动加载 `system_prompts/{mode}.txt` 专项 system prompt |
| `group_count` | **剧情组数 / 拼接段数**（新增）。设为 `1` = 单段（与原节点完全一致）。设为 `N (≥2)` 时进入**多段拼接模式**：在 `user_prompt` 内用单独一行 `---` 分隔出 N 段剧情描述，本节点生成一个专门 system prompt，指令 vlm 节点产出 **N 组完整 H3 提示词**，每组输出为 `### Group k` + 标准 H3 三字段；且**第 i+1 组画面/情节必须衔接第 i 组结尾**（同一角色/场景/视觉风格连续、承接上一组末帧动作、保留未解决的悬念钩子）。以此类推可生成无限组，N 组依次拼接即构成一条长视频，主要用于适配导演台插件，可用String Split节点将长提示词文本分割，实现一键长视频。
| `user_prompt` | **主提示词**：场景 / 角色 / 动作 / 台词。T2VA 必填；有图模式（I2VA 等）可留空，只把参考图接给 vlm 节点的 `images` 输入 |
| `duration_seconds` | 视频时长，注入 system prompt 的 Target Video Parameters 块 |
| `视觉风格` | 下拉选择（39 项：电影感/实拍/复古胶片/黑白电影/纪录片/极简广告/微距/航拍/二维动画/三维CG/日系二次元/美式漫画/皮克斯3D/定格动画/手绘发光/像素艺术/赛博朋克/蒸汽朋克/故障艺术/羊毛毡/折纸/水彩/粘土动画/水墨/油画/纸艺拼贴/剪纸/铅笔素描/浮世绘/敦煌壁画/青花瓷/工笔画/皮影戏/中国风插画/年画/布艺/蜡笔画/哥特萝莉）。选中后强制 `[Shot 1]` 以英文风格名开头并展开具体视觉描述；"GLOBAL MATERIAL OVERRIDE" 风格（水墨/粘土/羊毛毡等）会整体替换世界材质 |
| `音乐风格` | 下拉选择（22 项：禁止音乐/不指定/钢琴/管弦乐/原声吉他/电子/氛围/合成器浪潮/芯片音乐/Lo-fi/史诗/悬疑/浪漫弦乐/摇滚/爵士/嘻哈/放克/纯人声合唱/极简拟音/国风民乐/戏曲/古琴）。「禁止音乐」强制 `non_diegetic_music: N/A` |
| `use_api` | **是否直接用 API 生成提示词**（新增）。设为 yes 且 `api_url` 填了正确的 OpenAI 兼容地址时，`final_prompt` 输出口直接产出 H3 提示词；否则 `final_prompt` 输出空字符串，原因通过 `api_status` 输出口给出（中文提示，**不报错**）。 |
| `api_url` | **OpenAI 兼容 API 地址**（新增）。如 `http://localhost:8080/v1`（llama.cpp server）。支持带 `/v1` 或不带两种写法，**可省略 `http://` 前缀（自动补全）**。地址错误/为空时 `final_prompt` 为空、`api_status` 提示原因，不抛异常。 |
| `use_random_seed` | **随机种子开关**（新增）。yes=每次生成结果不同（不传 seed 给 API）；no=固定使用下方 `seed` 值，结果可复现。 |
| `seed` | **固定种子值**（新增，0–4294967295，默认 42）。`use_random_seed=no` 时生效。 |
| `system_extra` | **追加自定义规则**：拼接到 system prompt 尾部（如"整片保持胶片颗粒质感"） |
| `image` | **参考图**（新增，可选，IMAGE 类型）。`use_api=yes` 时按 OpenAI 多模态格式传给 API 服务器|

### 输出

| 输出 | 说明 |
|---|---|
| `system_prompt` | 完整 system prompt（模式专项 + Target Video Parameters 参数块 + 多段拼接规则 + 用户追加规则）→ 接 `llama_cpp_instruct_adv.system_prompt` |
| `user_prompt` | 主提示词原文（有图模式留空时输出占位指令）→ 接 `llama_cpp_instruct_adv.custom_prompt` |
| `final_prompt` | **API 直出提示词**。`use_api=yes` 且 `api_url` 正确时，直接产出最终 H3 提示词（含 `### Group k` 多段结构）；否则为空字符串 |
| `api_status` | **API 状态提示**。成功时 `OK (xx.xs)`；未启用/地址为空/地址错误/请求失败时给原因 |

### 外部 API 鉴权（新增）

`api_url` + `api_key` + `model_name` 三件套支持任意 OpenAI 兼容外部服务：
- `api_key` 非空时，请求自动带 `Authorization: Bearer <key>` 头（OpenAI / 中转站等需要鉴权的服务）；
- `model_name` 非空时，请求体携带 `model` 字段（OpenAI 必需；本地 llama.cpp 可不填）；
- 本地局域网 llama.cpp（如 `http://localhost:8080/v1`）两个都可留空。

## 排队预生成（入队即生成，不等执行）

**解决的问题**：ComfyUI 的任务队列是串行执行的——前面有任务在跑时，后面的任务要等它跑完才轮到；而提示词助手的 LLM 调用（约 4–7s）发生在"轮到该任务执行时"，白白加长关键路径。

**现在的行为**：点击 **Queue / Queue Front / Run** 入队时，前端立即把图中所有启用了 `use_api` 的提示词助手节点（四个节点都支持）的参数发给后端路由 `POST /h3promptgen/pregen`；后端在**后台线程**里立刻调 LLM 生成提示词并写入缓存（入队不受阻塞，LLM 与前面正在跑的任务并行）。等任务真正执行到该节点时，从缓存命中**秒回**，不再重复调 LLM——LLM 耗时被移出执行链，缩短总排队时间。

要点：
  - **局限性**：若参考图来自**上游生成的图**（VAE Encode / 采样器等输出），入队时该图还不存在，无法预生成——这类节点仍按原逻辑在轮到执行时调 API（约 1 分钟，属正常）；如需预生成收益，请让参考图走 Load Image。
- **自动跳过**：`use_api=no`、`api_url` 为空、或参考图来源无法解析（上游生成图）的节点不预生成。
- **失败降级**：预生成失败（LLM 不可达、图片缺失等）静默丢弃，正式执行时仍按原逻辑正常调 API——行为与旧版一致。
- **缓存上限**：每键最多 32 条、全局最多 512 个键（FIFO 淘汰），长时间运行不膨胀。
- **预期收益说明**：预生成只对**排队中**的任务有效（入队时前面已有任务在跑，LLM 并行进行）；**第一个任务**入队即执行，没有提前量，仍需等 LLM 调用（约 1 分钟属正常）。想给第一个任务也提速，可先点 Queue 排一个长任务，再把提示词任务排在后面。

## 多段剧情拼接（生成长视频）
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
   或
   生成N组提示词，视频内容是：*******。
4. 节点会在 system prompt 里追加 `# Multi-Sequence Continuation` 规则，指令 vlm 节点产出 `### Group 1 / Group 2 / Group 3 ...` 共 N 组**完整** H3 提示词，每组含 `integrated_multimodal_description / overall_soundscape / non_diegetic_music`。
5. vlm 节点（`llama_cpp_instruct_adv`）输出这 N 组后，按顺序分别喂给 N 个提示词框，即拼接成一条 N 段连续长视频。

### 衔接保证（系统提示词已硬性约束 vlm）
- **尾帧衔接**：第 i+1 组的第一个镜头必须接上第 i 组结尾（同一姿态/地点/光线/画面道具/机位）。
- **身份一致**：角色、服装、名称、视觉风格跨组保持不变。
- **钩子延续**：上一组结尾的悬念在下一组开头被承接或升级。
- **风格/音乐一致**：各组沿用相同的 `视觉风格` / `音乐风格`（除非用户分指定）。
- **时间戳**：每组内部 `[Shot 1]` 不带时间戳，后续镜头在该组的 `duration_seconds` 内严格递增。

> 注：真正的 N 组生成由 Llama-cpp_vlm 完成；本节点只负责把「多段拼接规则」注入 system prompt。多段模式若 `user_prompt` 为空（无 `---` 分隔），会抛 `ValueError` 提示。

## API 直出模式（无需 vlm 节点）

节点新增 `use_api` 开关 + `api_url` 输入 + `final_prompt` / `api_status` 两个输出口：

1. **`use_api` 设为 yes + 填对 `api_url`** → `final_prompt` 输出口**直接产出**最终 H3 提示词（单段一个 prompt；多段为 `### Group k` 结构）。适合把本节点当纯提示词 AI 助手用（如连你笔记本上的 llama.cpp：`http://localhost:8080/v1`）。
2. **`use_api` 为 no** → `final_prompt` 为空，`api_status` 提示 "use_api=no..."，行为与上一致（输出 system_prompt + user_prompt）。
3. **`use_api=yes` 但地址为空/错误/不可达** → `final_prompt` 为空，`api_status` 给出中文原因（"api_url 为空" / "无法连接 API: ..." / "API 返回 HTTP 4xx"）。
