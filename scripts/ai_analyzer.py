#!/usr/bin/env python3
"""新闻联播 AI 分析模块 — 使用 DeepSeek API（带缓存，每天只分析一次）"""

import json
import os
import re
from typing import Optional
from pathlib import Path

from openai import OpenAI
from dotenv import load_dotenv

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent
_CACHE_DIR = _PROJECT_ROOT / "data" / "analysis_cache"

# 加载 .env
_env_loaded = False


def _ensure_env():
    global _env_loaded
    if not _env_loaded:
        load_dotenv()
        _env_loaded = True


def get_deepseek_client() -> Optional[OpenAI]:
    """创建 DeepSeek 客户端（从 .env 读取 API Key）"""
    _ensure_env()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )


ANALYSIS_SYSTEM_PROMPT = """你是一位资深的《新闻联播》分析专家。你会收到当天新闻联播的完整文字稿，需要输出深入的结构化分析。

对每条新闻，需要输出：
1. **summary**: 准确概括本条新闻的核心内容和要点
2. **category**: 分类标签，如"政治""经济""科技""民生""外交""军事""文化""生态""法治""体育"
3. **keywords**: 关键词列表（3-5个）
4. **analysis**: 深入分析——本条新闻的背景意义、政策导向、或对普通人生活的影响（不超过1000字）

最后输出整体分析：
1. **overview**: 今日新闻联播整体概述，总结今日政策基调和重点方向
2. **key_themes**: 今日核心主题（3-5个）

必须以严格的 JSON 格式输出。"""

ANALYSIS_USER_PROMPT = """请分析以下《新闻联播》内容，输出结构化分析结果。

新闻列表（共{count}条）：
{items_text}

输出格式（严格 JSON，不要包含其他文字）：
{{
  "items": [
    {{"index": 1, "summary": "...", "category": "...", "keywords": ["...", "..."], "analysis": "..."}},
    ...
  ],
  "overview": "...",
  "key_themes": ["...", "..."]
}}"""


def _build_items_text(items: list) -> str:
    """将新闻列表拼接成 AI 可读的文本（全文发送）"""
    parts = []
    for i, item in enumerate(items, 1):
        title = item.get("title", "")
        content = item.get("content", "") or ""
        clean = re.sub(r'^央视网消息（新闻联播）[：:]\s*', '', content)
        if not clean:
            clean = content
        parts.append(f"[新闻{i}]\n标题：{title}\n内容：{clean}")
    return "\n\n".join(parts)


def _cache_path(run_key: str) -> Path:
    """获取 AI 分析缓存路径（按运行日期）"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"xwlb_analysis_{run_key}.json"


def _load_cached(run_key: str) -> Optional[dict]:
    """读取当天缓存"""
    path = _cache_path(run_key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(run_key: str, result: dict):
    """保存当天缓存"""
    path = _cache_path(run_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def analyze_xwlb(items: list, run_key: str = None) -> Optional[dict]:
    """调用 DeepSeek 分析新闻联播，按运行日期缓存（每天只分析一次）。

    Args:
        items: 新闻列表
        run_key: 运行日期，YYYYMMDD 格式

    Returns:
        分析结果 dict
    """
    if not items:
        return None

    # 尝试读取当天缓存
    if run_key:
        cached = _load_cached(run_key)
        if cached is not None:
            return cached

    # 没有缓存 → 调用 API
    client = get_deepseek_client()
    if not client:
        return None

    items_text = _build_items_text(items)
    user_prompt = ANALYSIS_USER_PROMPT.format(count=len(items), items_text=items_text)

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=8192,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        result = json.loads(text)

        # 保存当天缓存
        if run_key:
            _save_cache(run_key, result)

        return result
    except Exception as e:
        return {"error": str(e)}


def format_analysis_html(result: dict) -> str:
    """将分析结果格式化为 HTML"""
    if not result:
        return """
        <div class="xwlb-ai-placeholder">
            <div class="xwlb-ai-icon">⚠️</div>
            <div class="xwlb-ai-text">API 未配置</div>
            <div class="xwlb-ai-hint">请在 .env 中设置 DEEPSEEK_API_KEY</div>
        </div>"""

    if "error" in result:
        return f"""
        <div class="xwlb-ai-placeholder">
            <div class="xwlb-ai-icon">❌</div>
            <div class="xwlb-ai-text">分析出错</div>
            <div class="xwlb-ai-hint">{result['error'][:100]}</div>
        </div>"""

    items_data = result.get("items", [])
    overview = result.get("overview", "")
    key_themes = result.get("key_themes", [])

    # 构建概览区
    overview_html = ""
    if overview:
        overview_html = f"""
        <div class="xwlb-ai-section">
            <div class="xwlb-ai-section-title">📋 今日概览</div>
            <div class="xwlb-ai-overview">{overview}</div>
        </div>"""

    # 核心主题
    themes_html = ""
    if key_themes:
        themes_list = "".join(f'<span class="xwlb-ai-tag">{t}</span>' for t in key_themes)
        themes_html = f"""
        <div class="xwlb-ai-section">
            <div class="xwlb-ai-section-title">🎯 核心主题</div>
            <div class="xwlb-ai-themes">{themes_list}</div>
        </div>"""

    # 每条分析
    items_html = ""
    for idx, item in enumerate(items_data):
        summary = item.get("summary", "")
        category = item.get("category", "")
        keywords = item.get("keywords", [])
        analysis = item.get("analysis", "")
        kw_html = " ".join(f'<span class="xwlb-ai-kw">{k}</span>' for k in keywords)

        items_html += f"""
        <div class="xwlb-ai-item">
            <div class="xwlb-ai-item-header">
                <span class="xwlb-ai-idx">#{idx + 1}</span>
                <span class="xwlb-ai-cat">{category}</span>
            </div>
            <div class="xwlb-ai-summary">{summary}</div>
            {f'<div class="xwlb-ai-analysis">{analysis}</div>' if analysis else ''}
            {f'<div class="xwlb-ai-kws">{kw_html}</div>' if kw_html else ''}
        </div>"""

    return f"""
    <div class="xwlb-ai-content">
        {overview_html}
        {themes_html}
        <div class="xwlb-ai-section">
            <div class="xwlb-ai-section-title">📑 逐条分析</div>
            {items_html}
        </div>
    </div>"""
