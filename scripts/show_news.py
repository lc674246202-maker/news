#!/usr/bin/env python3
"""Horizon 新闻看板 — 国内/国外分类 + 标题翻译 + 展开全部"""

import asyncio
import sys
import re
import webbrowser
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import httpx
from src.models import RSSSourceConfig
from src.scrapers.rss import RSSScraper
from scripts.cookie_sources import fetch_all_cookie_sources
from scripts.xwlb_source import fetch_xwlb
from scripts.ai_analyzer import analyze_xwlb, format_analysis_html


# ── 翻译 ──
_trans_cache = {}

async def translate(text: str, client: httpx.AsyncClient) -> str:
    """通过 Google 免费 API 翻译英文→中文"""
    if not text or len(text) < 3:
        return ""
    text = text.strip()
    if text in _trans_cache:
        return _trans_cache[text]
    try:
        r = await client.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "zh-CN",
                    "dt": "t", "q": text[:500]},
            timeout=8,
        )
        result = r.json()[0][0][0]
        _trans_cache[text] = result
        return result
    except Exception:
        return ""


# ── 从文章页面提取真实发布时间 ──
_TIME_CACHE = {}
_DATE_PATTERNS = [
    # article:published_time
    re.compile(r'<meta\s+property="article:published_time"\s+content="([^"]+)"', re.I),
    re.compile(r'<meta\s+content="([^"]+)"\s+property="article:published_time"', re.I),
    # article:modified_time
    re.compile(r'<meta\s+property="article:modified_time"\s+content="([^"]+)"', re.I),
    re.compile(r'<meta\s+content="([^"]+)"\s+property="article:modified_time"', re.I),
    # pubdate / dc.date
    re.compile(r'<meta\s+name="(?:pubdate|dc\.date|date)"\s+content="([^"]+)"', re.I),
    re.compile(r'<meta\s+content="([^"]+)"\s+name="(?:pubdate|dc\.date|date)"', re.I),
    # <time datetime="...">
    re.compile(r'<time\s+[^>]*datetime="([^"]+)"', re.I),
    # schema.org JSON-LD
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
]

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%:z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


async def extract_article_time(url: str, client: httpx.AsyncClient) -> datetime | None:
    """抓取文章页面，从 meta 标签提取发布时间"""
    if url in _TIME_CACHE:
        return _TIME_CACHE[url]
    try:
        resp = await client.get(url, follow_redirects=True, timeout=10.0,
                                headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text
        for pattern in _DATE_PATTERNS:
            m = pattern.search(html)
            if m:
                date_str = m.group(1).strip()
                # 清理时区格式（Python 3.14 支持 %:z 但有些格式不标准）
                date_str = date_str.replace("+00:00", "+0000").replace("Z", "+0000")
                for fmt in _DATE_FORMATS:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        _TIME_CACHE[url] = dt
                        return dt
                    except ValueError:
                        continue
    except Exception:
        pass
    _TIME_CACHE[url] = None
    return None


# ── 工具函数 ──
def _title(item): return item["title"] if isinstance(item, dict) else (item.title or "无标题")
def _url(item):   return item["url"]   if isinstance(item, dict) else str(item.url)
def _time(item):
    if isinstance(item, dict): return item.get("time", "")
    return item.published_at.strftime("%m/%d %H:%M") if item.published_at else ""
def _meta(item):  return item.get("meta","") if isinstance(item, dict) else ""

def _content(item): return item.get("content","") if isinstance(item, dict) else (item.content or "")
DOMESTIC_NAMES = [
    "联合早报", "联合早报·国际", "澎湃新闻", "知乎热榜",
    "微博热搜", "B站热搜", "少数派",
    "量子位", "新智元", "V2EX 热门",
    "Linux DO", "NodeSeek",
]
FOREIGN_NAMES = [
    "Hacker News", "Simon Willison", "LWN.net", "Schneier Security",
    "CSS-Tricks", "Hackaday", "Nature", "Quanta Magazine",
    "Ars Technica", "The Verge",
]
NEWS_NAMES = ["新闻联播"]  # 新闻联播——后续添加


def render_source_block(source_name: str, items: list, color: dict,
                        translations: dict = None, inner_only: bool = False,
                        is_news: bool = False, analysis_html: str = None) -> str:
    """渲染一个新闻源列"""
    if is_news:
        return _render_news_block(source_name, items, color, inner_only, analysis_html)

    has_more = len(items) > 10
    items_html = ""
    for j, item in enumerate(items):
        title = _title(item)
        url = _url(item)
        pub_time = _time(item)
        meta = _meta(item)
        trans = (translations or {}).get(title, "")
        extra_cls = " item-more" if j >= 10 else ""

        items_html += f"""
        <div class="item{extra_cls}" data-source="{source_name}">
            <span class="ir">{j+1}</span>
            <a class="ib" href="{url}" target="_blank" rel="noopener">
                <span class="it">{title}</span>
                {f'<span class="it-trans">{trans}</span>' if trans else ''}
                <span class="im">{pub_time}{' · '+meta if meta else ''}</span>
            </a>
        </div>"""

    more_btn = ""
    if has_more:
        more_btn = f"""
        <div class="more-wrap" data-source="{source_name}">
            <button class="more-btn" onclick="toggleMore('{source_name}')">
                展开全部 {len(items)} 条 ▾
            </button>
        </div>"""

    inner = f"""
        <div class="ch">
            <span>{color['icon']} {source_name}</span>
            <span class="cc">{len(items)}</span>
        </div>
        <div class="ci">{items_html}{more_btn}</div>"""

    if inner_only:
        return inner

    return f"""
    <div class="col" style="--a:{color['accent']};--l:{color['light']};--b:{color['border']}">
        {inner}
    </div>"""


def _render_news_block(source_name: str, items: list, color: dict,
                       inner_only: bool = False, analysis_html: str = None) -> str:
    """渲染新闻联播 2 列布局：左=原文全文，右=AI分析（占位）"""
    left_html = ""
    for j, item in enumerate(items):
        title = _title(item)
        url = _url(item)
        content = _content(item)
        # 清理内容：去掉"央视网消息（新闻联播）"前缀
        content_clean = re.sub(r'^央视网消息（新闻联播）[：:]\s*', '', content)
        if not content_clean:
            content_clean = content

        # 判断是否是"快讯"类条目
        is_brief = '快讯' in title

        # 格式化正文：快讯类条目对子标题加粗
        if is_brief and content_clean:
            formatted_text = _format_xwlb_brief(content_clean)
        else:
            formatted_text = f'<div class="xwlb-text">{content_clean}</div>'

        left_html += f"""
        <div class="xwlb-item{' xwlb-brief' if is_brief else ''}">
            <div class="xwlb-num">{j+1}</div>
            <div class="xwlb-body">
                <a class="xwlb-title" href="{url}" target="_blank" rel="noopener">{title}</a>
                {formatted_text}
            </div>
        </div>"""

    _fallback = '<div class="xwlb-ai-placeholder"><div class="xwlb-ai-icon">\U0001f9e0</div><div class="xwlb-ai-text">AI 分析功能尚未接入</div><div class="xwlb-ai-hint">接入后将自动生成新闻摘要、分类、关键词等</div></div>'
    ai_col = analysis_html or _fallback
    inner = f"""
    <div class="xwlb-layout">
        <div class="xwlb-col xwlb-original">
            <div class="xwlb-col-header">\U0001f4f0 新闻联播原文</div>
            <div class="xwlb-list">{left_html}</div>
        </div>
        <div class="xwlb-col xwlb-ai">
            <div class="xwlb-col-header">\U0001f916 AI 分析</div>
            {ai_col}
        </div>
    </div>"""

    if inner_only:
        return inner

    return f"""
    <div class="col" style="--a:{color['accent']};--l:{color['light']};--b:{color['border']}">
        {inner}
    </div>"""


def _format_xwlb_brief(text: str) -> str:
    """格式化联播快讯内容：对子标题加粗醒目显示"""
    lines = text.split('\n')
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 匹配以 ◆、★、•、●、- 或 中文数字"一、二、三..."、"第X个" 开头的子标题
        # 或者"央视网消息"、"快讯"开头等
        # 更通用的：短句（<30字）且以特定标点结尾或没有标点
        clean = re.sub(r'\s+', ' ', line)
        # 如果一行字符数较少（通常是子标题），加粗显示
        if len(clean) < 40 and re.search(r'[。！？]$', clean):
            # 这是一句完整的短句，可能是快讯中的子标题
            html_parts.append(f'<div class="xwlb-brief-item">◆ {clean}</div>')
        elif len(clean) < 40 and not re.search(r'[。！？，、；：，]', clean[-1]):
            html_parts.append(f'<div class="xwlb-brief-item">◆ {clean}</div>')
        else:
            html_parts.append(f'<span class="xwlb-text">{line}</span>')

    if not html_parts:
        return f'<div class="xwlb-text">{text}</div>'

    result = '<div class="xwlb-brief-wrap">' + '\n'.join(html_parts) + '</div>'
    return result

def generate_html(domestic: dict, foreign: dict, news: dict, trans: dict,
                  all_items: dict, xwlb_analysis: str = None) -> str:
    now = datetime.now(timezone.utc).strftime("%m/%d %H:%M")
    total = sum(len(v) for v in all_items.values())

    color_list = [
        {"accent":"#e74c3c","light":"#fdf2f2","border":"#f5c6c6","icon":"🔶"},
        {"accent":"#7c3aed","light":"#f5f3ff","border":"#d5ccff","icon":"🟣"},
        {"accent":"#059669","light":"#ecfdf5","border":"#b8e6d3","icon":"🟢"},
        {"accent":"#0284c7","light":"#f0f9ff","border":"#b8dff5","icon":"🔵"},
        {"accent":"#d97706","light":"#fffbeb","border":"#fdebb3","icon":"🟡"},
        {"accent":"#dc2626","light":"#fef2f2","border":"#f5c2c2","icon":"🔴"},
        {"accent":"#0891b2","light":"#ecfeff","border":"#a5f3fc","icon":"🩵"},
        {"accent":"#9333ea","light":"#faf5ff","border":"#e1ccff","icon":"🟣"},
        {"accent":"#ea580c","light":"#fff7ed","border":"#fed7aa","icon":"🟠"},
        {"accent":"#2563eb","light":"#eff6ff","border":"#bfdbfe","icon":"🔷"},
        {"accent":"#65a30d","light":"#f7fee7","border":"#d9f99d","icon":"💚"},
        {"accent":"#db2777","light":"#fdf2f8","border":"#fbcfe8","icon":"💗"},
        {"accent":"#0d9488","light":"#f0fdfa","border":"#99f6e4","icon":"💎"},
        {"accent":"#4f46e5","light":"#eef2ff","border":"#c7d2fe","icon":"💙"},
    ]

    # ── 构建所有源的颜色映射 ──
    all_names = list(domestic.keys()) + list(foreign.keys()) + list(news.keys())
    color_map = {}
    for i, name in enumerate(all_names):
        color_map[name] = color_list[i % len(color_list)]

    # ── 侧边栏（按 tab 分组） ──
    def sidebar_group(title: str, names: list, tab_id: str) -> str:
        items_html = ""
        for idx, name in enumerate(names):
            c = color_map.get(name, color_list[0])
            items_html += f"""
            <div class="sl-item" draggable="true"
                 data-name="{name}" data-tab="{tab_id}" data-idx="{idx}"
                 ondragstart="onDragStart(event)"
                 ondragover="onDragOver(event)"
                 ondrop="onDrop(event)"
                 ondragend="onDragEnd(event)">
                <span class="sl-drag">⠿</span>
                <label class="sl-label" onclick="event.stopPropagation()">
                    <input type="checkbox" checked onchange="toggleSource('{name}',this.checked)">
                    <span class="sl-dot" style="background:{c['accent']}"></span>
                    <span class="sl-name">{name}</span>
                </label>
            </div>"""
        icon = {'domestic': '🇨🇳', 'foreign': '🌍', 'news': '📺'}.get(tab_id, '📡')
        return f"""
        <div class="sl-group" data-tab="{tab_id}">
            <div class="sl-group-title">{icon} {title}</div>
            {items_html}
        </div>"""

    sidebar_html = (
        sidebar_group("国内资讯", list(domestic.keys()), "domestic") +
        sidebar_group("国外资讯", list(foreign.keys()), "foreign") +
        sidebar_group("新闻联播", list(news.keys()), "news")
    )

    # ── 所有源的数据嵌入 JSON ──
    import json as _json
    source_dat = {}
    for name, items in all_items.items():
        c = color_map.get(name, color_list[0])
        if name in news:
            tab = "news"
        elif name in domestic:
            tab = "domestic"
        else:
            tab = "foreign"
        source_dat[name] = {
            "inner": render_source_block(name, items, c, trans.get(name), inner_only=True,
                                          is_news=(name in NEWS_NAMES),
                                          analysis_html=(xwlb_analysis if name in NEWS_NAMES else None)),
            "accent": c['accent'], "light": c['light'], "border": c['border'], "icon": c['icon'],
            "count": len(items),
            "tab": tab,
        }
    source_data_json = _json.dumps(source_dat, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新闻看板</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:#f4f5f7;color:#333;line-height:1.4}}
body{{display:flex;min-height:100vh}}

/* ===== 侧边栏 ===== */
.sidebar{{width:220px;flex-shrink:0;background:#fff;border-right:1px solid #e8e8e8;padding:10px 0;overflow-y:auto;height:100vh;position:sticky;top:0}}
.sidebar-title{{padding:8px 14px;font-size:13px;font-weight:700;color:#667eea;margin-bottom:4px}}
.sl-group{{margin-bottom:6px}}
.sl-group-title{{padding:4px 14px 4px;font-size:11px;color:#999;font-weight:600}}
.sl-item{{display:flex;align-items:center;gap:4px;padding:3px 14px 3px 8px;border-radius:4px;margin:1px 6px;cursor:default;transition:background .1s}}
.sl-item:hover{{background:#f0f2ff}}
.sl-item.dragging{{opacity:.5;background:#e8ecf8}}
.sl-item.drag-over{{border-top:2px solid #667eea}}
.sl-drag{{cursor:grab;font-size:14px;color:#bbb;flex-shrink:0;width:16px;text-align:center;user-select:none}}
.sl-drag:active{{cursor:grabbing}}
.sl-label{{display:flex;align-items:center;gap:5px;flex:1;min-width:0;cursor:pointer;padding:2px 0}}
.sl-label input[type=checkbox]{{accent-color:#667eea;width:13px;height:13px;cursor:pointer;flex-shrink:0}}
.sl-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.sl-name{{font-size:12px;color:#444;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

/* ===== 主区域 ===== */
.main{{flex:1;min-width:0;padding:10px 14px}}
.hd{{display:flex;align-items:baseline;justify-content:center;gap:12px;padding:10px 0 6px;flex-wrap:wrap}}
.hd h1{{font-size:20px;font-weight:700;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.hd .credit{{font-size:11px;color:#bbb}}
.stats{{display:flex;gap:14px;margin-bottom:8px;padding:4px 12px;background:#fff;border-radius:8px;font-size:11px;color:#888;justify-content:center}}

/* Tab */
.tabs{{display:flex;justify-content:center;gap:6px;margin-bottom:8px}}
.tab-btn{{padding:6px 20px;border:2px solid transparent;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;background:#eee;color:#888;transition:all .15s}}
.tab-btn:hover{{background:#e0e0e0}}
.tab-btn.active{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}}
.tab-btn .tab-count{{font-size:11px;font-weight:400;opacity:.8;margin-left:4px}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}

/* 网格 — 等高 */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:8px}}
.col{{background:var(--l);border:1px solid var(--b);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.ci{{padding:3px;flex:1}}
.ch{{display:flex;align-items:center;gap:6px;padding:7px 10px;background:var(--a);color:#fff;font-size:12px;font-weight:600;flex-shrink:0}}
.cc{{margin-left:auto;font-size:10px;opacity:.8;background:rgba(255,255,255,.2);padding:1px 7px;border-radius:7px}}
.hidden-source{{display:none!important}}
.col[data-name="新闻联播"]{{grid-column:1/-1}}

/* 单条 */
.item{{display:flex;gap:6px;padding:5px 8px;margin:2px 0;background:#fff;border-radius:5px;align-items:flex-start}}
.item:hover{{box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.ir{{flex-shrink:0;width:18px;height:18px;border-radius:3px;background:var(--l);color:var(--a);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-top:1px}}
.ib{{flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:2px 6px;align-items:baseline;text-decoration:none;color:inherit}}
.it{{font-size:13px;font-weight:500;color:#1a1a2e;line-height:1.35;word-break:break-word;width:100%}}
.ib:hover .it{{color:var(--a)}}
.it-trans{{font-size:11px;color:#888;line-height:1.3;width:100%;padding-left:2px}}
.im{{font-size:10px;color:#bbb;white-space:nowrap;flex-shrink:0}}
.more-wrap{{text-align:center;padding:6px 0 8px;flex-shrink:0}}
.more-btn{{border:none;background:var(--l);color:var(--a);font-size:11px;cursor:pointer;padding:4px 14px;border-radius:6px;font-weight:500}}
.more-btn:hover{{background:var(--b)}}
.item-more{{display:none}}
.item-more.show{{display:flex}}
.ft{{text-align:center;padding:14px 0 6px;color:#ccc;font-size:11px}}

/* ===== 新闻联播 2 列布局 ===== */
.xwlb-layout{{display:flex;gap:12px;min-height:500px}}
.xwlb-col{{flex:1;min-width:0;display:flex;flex-direction:column}}
.xwlb-col-header{{font-size:14px;font-weight:700;padding:8px 12px;background:#fff;border-radius:8px 8px 0 0;border-bottom:2px solid #667eea;color:#333;flex-shrink:0}}
.xwlb-list{{flex:1;overflow-y:auto;padding:6px;background:#fff;border-radius:0 0 8px 8px}}
.xwlb-item{{display:flex;gap:8px;padding:10px 12px;margin:4px 0;background:#f8f9fb;border-radius:6px;border-left:3px solid #667eea}}
.xwlb-item:hover{{background:#f0f2ff}}
.xwlb-num{{flex-shrink:0;width:24px;height:24px;border-radius:6px;background:#667eea;color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}}
.xwlb-body{{flex:1;min-width:0}}
.xwlb-title{{display:block;font-size:14px;font-weight:600;color:#1a1a2e;text-decoration:none;margin-bottom:6px;line-height:1.4}}
.xwlb-title:hover{{color:#667eea}}
.xwlb-text{{font-size:13px;color:#555;line-height:1.7;white-space:pre-wrap;word-break:break-word}}
.xwlb-ai-placeholder{{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#fff;border-radius:0 0 8px 8px;padding:40px 20px;color:#bbb}}
.xwlb-ai-icon{{font-size:48px;margin-bottom:12px}}
.xwlb-ai-text{{font-size:15px;font-weight:600;color:#999;margin-bottom:6px}}
.xwlb-ai-hint{{font-size:12px;color:#ccc}}
@media(max-width:900px){{.sidebar{{display:none}}.main{{padding:10px}}.xwlb-layout{{flex-direction:column}}}}

/* ===== 新闻联播快讯特殊排版 ===== */
.xwlb-brief{{border-left-color:#f59e0b!important;background:#fffbeb!important}}
.xwlb-brief .xwlb-title{{color:#d97706}}
.xwlb-brief-wrap{{font-size:13px;line-height:1.7}}
.xwlb-brief-item{{font-weight:700;color:#92400e;padding:4px 0 2px 0;font-size:13px;border-bottom:1px dashed #fde68a;margin:4px 0}}

/* ===== AI 分析面板样式 ===== */
.xwlb-ai-content{{padding:8px 6px;flex:1;overflow-y:auto;background:#fff;border-radius:0 0 8px 8px}}
.xwlb-ai-section{{margin-bottom:12px}}
.xwlb-ai-section-title{{font-size:13px;font-weight:700;color:#333;margin-bottom:6px;padding:4px 8px;background:#f0f2ff;border-radius:4px}}
.xwlb-ai-overview{{font-size:13px;color:#555;line-height:1.6;padding:6px 8px}}
.xwlb-ai-themes{{display:flex;flex-wrap:wrap;gap:6px;padding:4px 8px}}
.xwlb-ai-tag{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:500;background:#e8ecf8;color:#4a5a9a}}
.xwlb-ai-item{{padding:8px;margin:4px 0;background:#f8f9fb;border-radius:6px}}
.xwlb-ai-item-header{{display:flex;align-items:center;gap:6px;margin-bottom:4px}}
.xwlb-ai-idx{{font-weight:700;color:#667eea;font-size:12px}}
.xwlb-ai-cat{{font-size:11px;padding:1px 8px;border-radius:8px;background:#e8ecf8;color:#4a5a9a}}
.xwlb-ai-summary{{font-size:13px;color:#444;line-height:1.5;margin-bottom:4px;font-weight:500}}
.xwlb-ai-analysis{{font-size:12px;color:#666;line-height:1.6;margin:4px 0 4px;padding:6px 8px;background:#f0f4ff;border-radius:4px;border-left:3px solid #667eea}}
.xwlb-ai-kws{{display:flex;flex-wrap:wrap;gap:4px}}
.xwlb-ai-kw{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:4px;background:#e6f7e6;color:#389e0d}}</style>
</head>
<body>

<!-- 侧边栏 -->
<div class="sidebar" id="sidebar">
    <div class="sidebar-title">▣ 选择信息源</div>
    {sidebar_html}
</div>

<!-- 主区域 -->
<div class="main">
<div class="hd">
    <h1>🌅 新闻看板</h1>
    <span class="credit">感谢 jxtan 和 Thysrael</span>
</div>
<div class="stats">📡 <span>{len(all_items)}</span> 源 · 📰 <span>{total}</span> 条</div>

<div class="tabs">
    <button class="tab-btn active" onclick="switchTab('domestic',this)">
        🇨🇳 国内 <span class="tab-count" id="cnt-domestic">{sum(len(v) for v in domestic.values())}</span>
    </button>
    <button class="tab-btn" onclick="switchTab('foreign',this)">
        🌍 国外 <span class="tab-count" id="cnt-foreign">{sum(len(v) for v in foreign.values())}</span>
    </button>
    <button class="tab-btn" onclick="switchTab('news',this)">
        📺 新闻联播 <span class="tab-count" id="cnt-news">{sum(len(v) for v in news.values())}</span>
    </button>
</div>

<div id="tab-domestic" class="tab-content active"><div class="grid" id="grid-domestic"></div></div>
<div id="tab-foreign" class="tab-content"><div class="grid" id="grid-foreign"></div></div>
<div id="tab-news" class="tab-content"><div class="grid" id="grid-news"></div></div>

<div class="ft">Powered by <strong>Horizon</strong></div>
</div>

<script>
// ── 全部源数据 ──
var SOURCE_DATA = {source_data_json};
var STATE_KEY = 'horizon_source_state';
var ORDER_KEY = 'horizon_source_order';
var currentTab = 'domestic';
var TAB_NAMES = {{domestic:'国内', foreign:'国外', news:'新闻联播'}};

// ── localStorage 工具 ──
function loadState() {{
    try {{ return JSON.parse(localStorage.getItem(STATE_KEY)) || {{}}; }} catch(e) {{ return {{}}; }}
}}
function saveState(s) {{
    try {{ localStorage.setItem(STATE_KEY, JSON.stringify(s)); }} catch(e) {{}}
}}
function loadOrder() {{
    try {{ return JSON.parse(localStorage.getItem(ORDER_KEY)); }} catch(e) {{ return null; }}
}}
function saveOrder(order) {{
    try {{ localStorage.setItem(ORDER_KEY, JSON.stringify(order)); }} catch(e) {{}}
}}

// ── 拖拽 ──
var dragSrc = null;
function onDragStart(e) {{
    dragSrc = e.currentTarget;
    e.currentTarget.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}}
function onDragOver(e) {{
    e.preventDefault();
    if (e.currentTarget !== dragSrc && e.currentTarget.dataset.tab === dragSrc.dataset.tab) {{
        e.currentTarget.classList.add('drag-over');
    }}
}}
function onDrop(e) {{
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    if (dragSrc && dragSrc.dataset.tab === e.currentTarget.dataset.tab) {{
        var parent = dragSrc.parentNode;
        var items = Array.from(parent.querySelectorAll('.sl-item'));
        var srcIdx = items.indexOf(dragSrc);
        var dstIdx = items.indexOf(e.currentTarget);
        if (srcIdx < dstIdx) {{
            parent.insertBefore(dragSrc, e.currentTarget.nextSibling);
        }} else {{
            parent.insertBefore(dragSrc, e.currentTarget);
        }}
        persistOrder();
        renderGrid(currentTab);
    }}
    dragSrc = null;
}}
function onDragEnd(e) {{
    e.currentTarget.classList.remove('dragging');
    document.querySelectorAll('.drag-over').forEach(function(el){{el.classList.remove('drag-over')}});
}}
function persistOrder() {{
    var order = [];
    document.querySelectorAll('.sl-item').forEach(function(el) {{
        order.push(el.dataset.name);
    }});
    saveOrder(order);
}}

// ── 侧边栏恢复顺序 ──
function restoreSidebarOrder() {{
    var order = loadOrder();
    if (!order) return;
    // 按 order 重排每个 sl-group 内的 .sl-item
    document.querySelectorAll('.sl-group').forEach(function(group) {{
        var items = Array.from(group.querySelectorAll('.sl-item'));
        var sorted = items.sort(function(a,b) {{
            return order.indexOf(a.dataset.name) - order.indexOf(b.dataset.name);
        }});
        sorted.forEach(function(el) {{ group.appendChild(el); }});
    }});
}}

// ── 切换可见性 ──
function toggleSource(name, visible) {{
    var state = loadState();
    state[name] = visible;
    saveState(state);
    var col = document.querySelector('.col[data-name="'+name+'"]');
    if (col) {{
        col.classList.toggle('hidden-source', !visible);
    }}
    updateCounts();
}}

// ── 恢复勾选状态 ──
function restoreState() {{
    var state = loadState();
    document.querySelectorAll('.sl-item').forEach(function(el) {{
        var name = el.dataset.name;
        var cb = el.querySelector('input[type=checkbox]');
        cb.checked = state[name] !== false;
    }});
}}

// ── 渲染网格 ──
function renderGrid(tab) {{
    var grid = document.getElementById('grid-'+tab);
    var state = loadState();
    // 从侧边栏读顺序
    var names = [];
    document.querySelectorAll('.sl-item[data-tab="'+tab+'"]').forEach(function(el) {{
        names.push(el.dataset.name);
    }});
    var html = '';
    names.forEach(function(name) {{
        var d = SOURCE_DATA[name];
        if (!d) return;
        var visible = state[name] !== false;
        var cls = visible ? '' : ' hidden-source';
        html += '<div class="col' + cls + '" data-name="' + name + '" style="--a:' + d.accent + ';--l:' + d.light + ';--b:' + d.border + '">'
             + d.inner + '</div>';
    }});
    grid.innerHTML = html;
    updateCounts();
}}

function updateCounts() {{
    var state = loadState();
    var counts = {{domestic:0, foreign:0, news:0}};
    Object.keys(SOURCE_DATA).forEach(function(name) {{
        if (state[name] === false) return;
        var sd = SOURCE_DATA[name];
        if (counts[sd.tab] !== undefined) counts[sd.tab] += sd.count;
    }});
    document.getElementById('cnt-domestic').textContent = counts.domestic;
    document.getElementById('cnt-foreign').textContent = counts.foreign;
    document.getElementById('cnt-news').textContent = counts.news;
}}

// ── Tab 切换 ──
function switchTab(tab, btn) {{
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active')}});
    document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active')}});
    btn.classList.add('active');
    document.getElementById('tab-'+tab).classList.add('active');
    renderGrid(tab);
}}

// ── 展开/收起 ──
function toggleMore(name) {{
    var items = document.querySelectorAll('.item[data-source="'+name+'"]');
    var btns = document.querySelectorAll('.more-btn');
    var btn = null;
    btns.forEach(function(b) {{
        if (b.getAttribute('onclick') && b.getAttribute('onclick').includes(name)) btn = b;
    }});
    var hidden = false;
    items.forEach(function(it) {{
        if (it.classList.contains('item-more')) {{
            it.classList.toggle('show');
            if (it.classList.contains('show')) hidden = true;
        }}
    }});
    if (btn) btn.textContent = hidden ? '收起 ▴' : '展开全部 ' + items.length + ' 条 ▾';
}}

// ── 初始化 ──
window.onload = function() {{
    restoreSidebarOrder();
    restoreState();
    renderGrid('domestic');
}};
</script>
</body>
</html>"""
    return html


async def main():
    print(f"\n🌅 Horizon 新闻看板")
    print(f"{'='*55}\n")

    # ── RSS 源列表 ──
    rss_sources = [
        # 国外
        RSSSourceConfig(name="Hacker News",      url="https://hnrss.org/frontpage"),
        RSSSourceConfig(name="Simon Willison",   url="https://simonwillison.net/atom/everything/"),
        RSSSourceConfig(name="LWN.net",          url="https://lwn.net/headlines/rss"),
        RSSSourceConfig(name="Schneier Security",url="https://www.schneier.com/feed/atom/"),
        RSSSourceConfig(name="CSS-Tricks",       url="https://css-tricks.com/feed/"),
        RSSSourceConfig(name="Hackaday",         url="https://hackaday.com/feed/"),
        RSSSourceConfig(name="Nature",           url="https://www.nature.com/nature.rss"),
        RSSSourceConfig(name="Quanta Magazine",  url="https://api.quantamagazine.org/feed/"),
        RSSSourceConfig(name="Ars Technica",     url="https://feeds.arstechnica.com/arstechnica/index"),
        RSSSourceConfig(name="The Verge",        url="https://www.theverge.com/rss/index.xml"),
        # 国内
        RSSSourceConfig(name="量子位",    url="https://wechat2rss.xlab.app/feed/7131b577c61365cb47e81000738c10d872685908.xml"),
        RSSSourceConfig(name="新智元",    url="https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"),
        RSSSourceConfig(name="V2EX 热门", url="https://rsshub.rssforever.com/v2ex/topics/hot"),
        RSSSourceConfig(name="少数派",    url="https://rsshub.rssforever.com/sspai/index"),
        RSSSourceConfig(name="联合早报",  url="https://rsshub.rssforever.com/zaobao/realtime/china"),
        RSSSourceConfig(name="联合早报·国际", url="https://rsshub.rssforever.com/zaobao/realtime/world"),
        RSSSourceConfig(name="澎湃新闻",  url="https://rsshub.rssforever.com/thepaper/featured"),
        RSSSourceConfig(name="Linux DO",  url="https://linux.do/latest.rss"),
        RSSSourceConfig(name="NodeSeek",  url="https://rss.nodeseek.com"),
        RSSSourceConfig(name="知乎热榜",  url="https://rsshub.rssforever.com/zhihu/hot"),
    ]

    since = datetime.now(timezone.utc) - timedelta(days=7)

    # ── 抓取 ──
    all_grouped = {}
    translated = {}  # {源名称: {原标题: 翻译}}

    async with httpx.AsyncClient(timeout=30.0) as client:
        scraper = RSSScraper(rss_sources, client)

        for source in rss_sources:
            total_count = 0
            print(f"   📡 {source.name:16s} ... ", end="", flush=True)
            try:
                items = await scraper._fetch_feed(source, since)
                items.sort(key=lambda x: x.published_at or datetime.min, reverse=True)
                total_count = len(items)
                if items:
                    print(f"✅ {total_count} 条")
                    all_grouped[source.name] = items
                else:
                    print(f"ℹ️  无")
            except Exception as e:
                print(f"❌ {str(e)[:25]}")

        # Cookie 源
        print()
        cookie_results = await fetch_all_cookie_sources(client)
        for name, items in cookie_results.items():
            print(f"   🔑 {name:16s} ... ✅ {len(items)} 条")
            all_grouped[name] = items

        # ── 从文章页面提取真实发布时间（Hacker News 为主） ──
        TIME_NEEDED = {"Hacker News"}
        time_items = [(name, it) for name, its in all_grouped.items()
                      if name in TIME_NEEDED for it in its[:30]]  # 最多取前 30 条
        if time_items:
            print(f"\n   🕐 从原文提取 {len(time_items)} 条真实发布时间...")
            tasks = [extract_article_time(str(it.url) if hasattr(it, 'url') else it['url'], client)
                     for name, it in time_items]
            results = await asyncio.gather(*tasks)
            fixed = 0
            for (name, it), article_time in zip(time_items, results):
                if article_time:
                    if hasattr(it, 'published_at') and article_time:
                        it.published_at = article_time
                        fixed += 1
            print(f"   ✅ 修正 {fixed} 条发布时间\n")

        # ── 新闻联播（昨天） ──
        print()
        xwlb_analysis = None
        try:
            xwlb_result = await fetch_xwlb(client=client)
            if xwlb_result.get("items") and xwlb_result["success"] > 0:
                xwlb_date = xwlb_result["date"]
                items = []
                for it in xwlb_result["items"]:
                    content = it.get("content", "") or ""
                    preview = re.sub(r'\s+', ' ', content).strip()[:120] if content else ""
                    items.append({
                        "title": it["title"],
                        "url": it["url"],
                        "time": "",
                        "meta": preview,
                        "content": content,  # 存全文
                    })
                print(f"   📺 新闻联播 ... ✅ {len(items)} 条 ({xwlb_date})")
                all_grouped["新闻联播"] = items
                # ── AI 分析（带缓存，新闻不变就不重复调用 API） ──
                print(f"   🤖 AI 分析新闻联播 {len(items)} 条...")
                try:
                    result = analyze_xwlb(items, run_key=datetime.now(timezone.utc).strftime("%Y%m%d"))
                    if result and "error" not in result:
                        print(f"   ✅ AI 分析完成")
                    elif result and "error" in result:
                        print(f"   ⚠️  AI 分析失败: {result['error'][:60]}")
                    xwlb_analysis = format_analysis_html(result)
                except Exception as e:
                    print(f"   ❌ AI 分析出错: {e}")
                    xwlb_analysis = None
            else:
                print(f"   📺 新闻联播 ... ❌ {xwlb_result.get('error', '无数据')}")
                xwlb_analysis = None
        except Exception as e:
            print(f"   📺 新闻联播 ... ❌ {e}")
            xwlb_analysis = None

    if not all_grouped:
        print("\n❌ 未获取到任何新闻")
        return

    # ── 翻译国外源标题 ──
    foreign_set = set(FOREIGN_NAMES)
    foreign_items = [(n, it) for n, its in all_grouped.items()
                     if n in foreign_set for it in its]
    if foreign_items:
        print(f"\n   🌍 翻译 {len(foreign_items)} 条国外标题...")
        async with httpx.AsyncClient(timeout=10.0) as tc:
            batch = []
            for name, item in foreign_items:
                title = _title(item)
                if title:
                    batch.append((name, item, title))
            # 并发翻译
            tasks = [translate(title, tc) for _, _, title in batch]
            results = await asyncio.gather(*tasks)
            for (name, item, title), result in zip(batch, results):
                if result:
                    translated.setdefault(name, {})[title] = result
        print(f"   ✅ 翻译完成\n")

    # ── 分国内/国外/新闻联播（按 DOMESTIC_NAMES/FOREIGN_NAMES 顺序插入） ──
    domestic = {}
    foreign = {}
    news = {}
    for name in DOMESTIC_NAMES:
        if name in all_grouped:
            domestic[name] = all_grouped[name]
    for name in FOREIGN_NAMES:
        if name in all_grouped:
            foreign[name] = all_grouped[name]
    for name in NEWS_NAMES:
        if name in all_grouped:
            news[name] = all_grouped[name]

    # 打印汇总
    total = sum(len(v) for v in all_grouped.values())
    d_total = sum(len(v) for v in domestic.values())
    f_total = sum(len(v) for v in foreign.values())
    n_total = sum(len(v) for v in news.values())
    print(f"   ✅ 共 {len(all_grouped)} 个源, {total} 条")
    print(f"      🇨🇳 国内: {len(domestic)} 源, {d_total} 条")
    print(f"      🌍 国外: {len(foreign)} 源, {f_total} 条")
    if news:
        print(f"      📺 新闻联播: {len(news)} 源, {n_total} 条")

    # ── 生成 HTML ──
    html_content = generate_html(domestic, foreign, news, translated, all_grouped, xwlb_analysis=xwlb_analysis)
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)
    html_path = output_dir / "news.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"\n   💾 {html_path}")

    file_url = f"file://{html_path.resolve()}"
    print(f"   🚀 打开浏览器...")
    webbrowser.open(file_url)
    print(f"\n{'='*55}  ✅ 完成\n")

if __name__ == "__main__":
    asyncio.run(main())
