# -*- coding: utf-8 -*-
"""comfyui-h3-prompt-gen — H3 提示词生成器 (方案2: 纯提示词工程)。

只做提示词工程: 输出 system_prompt + user_prompt, 接线 ComfyUI-llama-cpp_vlm 的
llama_cpp_instruct_adv 节点。模型加载/显存管理完全由 vlm 插件负责, 本插件不调用 LLM,
不安装任何 monkeypatch。
"""
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

# 新版 ComfyUI 不再自动加载自定义节点的 js/ 目录, 必须显式声明
# WEB_DIRECTORY (与 XB_ToolBox / ComfyUI-Manager 相同机制), 否则
# 前端 /extensions 不会列出 promptgen_mode.js, 面板重建与排队预生成不生效。
WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
