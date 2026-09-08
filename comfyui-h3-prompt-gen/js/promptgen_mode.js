/**
 * 提示词助手 (MiniMax/Krea-2/Z-Image/自定义) — 模式面板真实重建
 * =====================================================================
 * 与 XB_ToolBox xb_list_dispatcher.js 相同的 kjnodes 模式:
 *   - 切换 mode 时, 从节点上【彻底移除】当前模式的专属控件, 再按新模式
 *     重新创建控件 (ComfyWidgets), 而不是隐藏 —— 效果与 MiniMaxH3Director
 *     一样: 换模式 = 换专用面板, 非本模式控件在节点上不存在。
 *   - 控件定义全部取自后端 INPUT_TYPES (nodeData.input), 与 Python 端
 *     单一数据源, 避免 JS/Python 双份列表失步。
 *   - 值保留: 切换模式时保留同名字控件的值 (如 user_prompt / api_url),
 *     切回来时专属控件值也恢复。
 */

import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

const NODE_TYPE = "UnifiedPromptGenerator";
const DEFAULT_MODE = "Krea-2 (文生图)";

// ── 面板组成: 通用控件 + 各模式专属控件 ──
// 顺序 = 面板显示顺序 = 保存时 widgets_values 的顺序 (勿随意调整)
const COMMON = [
    "mode", "user_prompt",
    "use_api", "api_url", "api_key", "model_name",
    "use_random_seed", "seed",
    "system_extra",
];
const MODE_EXTRA = {
    "MiniMax H3 (视频)": ["h3_mode", "group_count", "duration_seconds", "视觉风格", "音乐风格"],
    "Krea-2 (文生图)": ["画面风格", "写实增强"],
    "Z-Image (文生图)": [],
    "自定义 / Custom": ["custom_system_prompt"],
};
const MANAGED = new Set([...COMMON, "control_after_generate", ...Object.values(MODE_EXTRA).flat()]);
// 控件定义缓存 (beforeRegisterNodeDef 时从后端 INPUT_TYPES 收集, 单一数据源)
const SPEC_CACHE = {};

/** 纯函数: 某模式的完整面板控件名列表 */
export function panelOrder(mode) {
    return [...COMMON, ...(MODE_EXTRA[mode] || [])];
}

/** 纯函数: 面板实际控件顺序 —— 前端会自动为 seed 控件追加 control_after_generate
 *  (新前端 seed 机制), 它固定紧跟 seed 之后; 面板保存/加载都按此顺序对齐。 */
export function panelWithCag(mode) {
    const order = panelOrder(mode);
    const i = order.indexOf("seed");
    if (i < 0) return order;
    return [...order.slice(0, i + 1), "control_after_generate", ...order.slice(i + 1)];
}

/** 纯函数: 把保存值数组按面板顺序映射为 {name: value} (截断到面板长度) */
export function mapSavedValues(mode, saved) {
    const order = panelOrder(mode);
    const out = {};
    if (!Array.isArray(saved)) return out;
    for (let i = 0; i < order.length && i < saved.length; i++) {
        if (saved[i] !== undefined && saved[i] !== null) out[order[i]] = saved[i];
    }
    return out;
}

function makeRebuild(node, specs) {
    return function rebuild(savedByName) {
        if (node._pgBusy) return;
        node._pgBusy = true;
        try {
            // 1. 快照当前值 (切换模式时保留同名控件值)
            const snap = {};
            for (const w of node.widgets || []) {
                if (MANAGED.has(w.name)) snap[w.name] = w.value;
            }
            // 2. 移除全部受管控件 (含前端自动加的 control_after_generate, 防止重复)
            for (let i = (node.widgets || []).length - 1; i >= 0; i--) {
                const w = node.widgets[i];
                if (MANAGED.has(w.name)) {
                    w.onRemove?.();
                    node.widgets.splice(i, 1);
                }
            }
            // 3. 确定模式: 加载时用保存值, 交互切换时用快照, 否则默认
            const mode = (savedByName && savedByName["mode"])
                ? savedByName["mode"] : (snap["mode"] || DEFAULT_MODE);
            // 4. 按新模式创建控件
            for (const name of panelWithCag(mode)) {
                if (name === "control_after_generate") continue; // 前端为 seed 自动添加
                const def = specs[name];
                if (!def) continue;
                let w = null;
                try {
                    // ComfyWidgets 以【控件类型】为键 (STRING/INT/FLOAT/BOOLEAN/COMBO...),
                    // 不是控件名。控件定义里 STRING 型为 ["STRING", {...}],
                    // COMBO 型为 [选项数组, {...}] (无类型前缀) → 需自行判定。
                    const type = (typeof def[0] === "string") ? def[0] : "COMBO";
                    w = ComfyWidgets[type](node, name, def, app).widget;
                } catch (e) {
                    console.warn("[promptgen_mode] create widget failed:", name, e);
                }
                if (!w) continue;
                // 值恢复: 保存值 > 快照 > 控件默认
                const v = (savedByName && savedByName[name] !== undefined) ? savedByName[name] : snap[name];
                if (v !== undefined) w.value = v;
                if (name === "mode") {
                    w.callback = () => {
                        if (node._pgLoading) return;
                        rebuild();
                    };
                }
            }
            node.setSize([node.size[0], node.computeSize()[1]]);
            node.graph?.setDirtyCanvas?.(true, true);
            // 5. 把保存的 control_after_generate 值应用回前端自动添加的 companion
            if (savedByName && savedByName["control_after_generate"] !== undefined) {
                const v = savedByName["control_after_generate"];
                setTimeout(() => {
                    const cag = (node.widgets || []).find(w => w.name === "control_after_generate" && w.type === "combo");
                    if (cag) cag.value = v;
                }, 150);
            }
        } finally {
            node._pgBusy = false;
        }
    };
}

app.registerExtension({
    name: "H3PromptGen.ModePanel",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;

        // 控件定义单一数据源: 全部取自已注册的 INPUT_TYPES (required + optional)
        const inputs = nodeData.input || {};
        const specs = {};
        for (const [name, def] of Object.entries({ ...(inputs.required || {}), ...(inputs.optional || {}) })) {
            if (MANAGED.has(name)) specs[name] = def;
        }
        SPEC_CACHE[NODE_TYPE] = specs;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            const rebuild = makeRebuild(this, specs);
            // 新节点拖入画布: configure 不触发, setTimeout 兜底立即收敛到默认面板
            setTimeout(() => rebuild(), 50);
            // 加载/粘贴工作流: 先让 configure 恢复值, 再按保存的 mode 重建面板
            const origConfigure = this.configure;
            this.configure = function (info) {
                const saved = (info && Array.isArray(info.widgets_values))
                    ? [...info.widgets_values] : null;
                this._pgLoading = true;
                try {
                    if (origConfigure) origConfigure.apply(this, arguments);
                } finally {
                    this._pgLoading = false;
                }
                let byName = null;
                if (saved && saved.length) {
                    byName = {};
                    const mode = saved[0];
                    if ((this.widgets || []).length === saved.length) {
                        // 旧版工作流 (保存时面板未重建, 全量 17+1 控件):
                        // 按 configure 之后的控件名对齐, 位置无关地恢复值
                        const ws = this.widgets || [];
                        for (let i = 0; i < ws.length && i < saved.length; i++) {
                            if (ws[i] && ws[i].name) byName[ws[i].name] = saved[i];
                        }
                    } else {
                        // 新版: saved 顺序 = 面板实际顺序 (含 control_after_generate)
                        const pw = panelWithCag(mode);
                        for (let i = 0; i < pw.length && i < saved.length; i++) {
                            byName[pw[i]] = saved[i];
                        }
                    }
                }
                rebuild(byName);
            };
            return r;
        };
    },

    async loadedGraphNode(node) {
        if (node.type !== NODE_TYPE) return;
        // 保险: 若 configure 未触发 (极端情况), 300ms 后按当前 mode 重建
        setTimeout(() => {
            makeRebuild(node, SPEC_CACHE[NODE_TYPE] || {})();
        }, 300);
    },

    // ── 排队预生成: 入队时立即让后端后台生成提示词, 不阻塞入队 ──
    // 后端 POST /h3promptgen/pregen → 后台线程调 LLM → 写缓存;
    // 任务真正执行到提示词节点时命中缓存秒回, LLM 耗时移出关键路径。
    setup() {
        const PREGEN_NODE_TYPES = new Set([
            "UnifiedPromptGenerator",
        ]);
        const resolveImageFiles = (graph, node) => {
            // 返回 null = 未接参考图; [] = 接了图但来源无法解析 (跳过预生成);
            // [文件...] = 可预生成 (前端把文件名交给后端, 后端从 input 目录加载)
            const inp = (node.inputs || []).find(i => i.name === "image");
            if (!inp || inp.link == null) return null;
            const getLink = (id) => {
                const m = graph.links;
                if (!m || id == null) return null;
                const v = (typeof m.get === "function") ? m.get(id) : m[id];
                if (!v) return null;
                // 新版 litegraph: links Map 值是 LLink 对象 {origin_id, target_id,...};
                // 旧版: [id, originId, originSlot, targetId, targetSlot, type] 数组 → 归一化
                if (Array.isArray(v)) return { origin_id: v[1], target_id: v[3] };
                return v;
            };
            const nodeById = (id) => (graph._nodes || []).find(n => String(n.id) === String(id));
            const files = [];
            const seen = new Set();
            const walk = (lnode, depth) => {
                if (!lnode || depth > 4) return;
                const t = lnode.type || "";
                if (t.includes("LoadImage") || t.includes("imagesLoader")) {   // 静态来源: 取文件名
                    const w = (lnode.widgets || []).find(w => w.name === "image");
                    if (w && w.value && !seen.has(w.value)) { seen.add(w.value); files.push(w.value); }
                    return;
                }
                if (t.includes("ImageBatch") || t.includes("ImageConcatenate")) { // 多图: 递归解析各输入
                    for (const i of (lnode.inputs || [])) {
                        if (i.link == null) continue;
                        const lk = getLink(i.link);
                        if (lk) walk(nodeById(lk.origin_id), depth + 1);
                    }
                    return;
                }
                // 其他上游 (生成图/VAE 输出): 入队时图片尚不存在, 无法预生成
            };
            const first = getLink(inp.link);
            if (first) walk(nodeById(first.origin_id), 0);
            return files.length ? files : [];
        };

        const firePregen = (batchCount) => {
            const graph = app.graph;
            if (!graph?._nodes) return;
            const count = Math.max(1, Math.min(batchCount || 1, 8));
            for (const node of graph._nodes) {
                if (!PREGEN_NODE_TYPES.has(node.type)) continue;
                if (!(node.widgets || []).length) continue;
                const inputs = {};
                for (const w of node.widgets) inputs[w.name] = w.value;
                if (!inputs.use_api || !inputs.api_url) continue;          // 未启用 API 无需预生成
                const imageFiles = resolveImageFiles(graph, node);
                if (imageFiles === null) {
                    // 未接图: 纯文本预生成
                } else if (imageFiles.length === 0) {
                    continue;  // 接了图但来源是上游生成图 (入队时不存在), 跳过
                }
                const body = JSON.stringify({ node_type: node.type, inputs, count, image_files: imageFiles });
                fetch("/h3promptgen/pregen", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body,
                }).catch(() => {});
            }
        };

        // 本版本前端没有 beforeEnqueuePrompt 钩子, 包装 app.queuePrompt
        // (Queue / Queue Front / 自动队列 / Run 都经过它)
        const origQueue = app.queuePrompt?.bind(app);
        if (typeof origQueue !== "function") return;
        app.queuePrompt = async function (number, batchCount, queueNodeIds) {
            firePregen(batchCount);
            return origQueue(number, batchCount, queueNodeIds);
        };
    },
});
