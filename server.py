#!/usr/bin/env python3
"""Horizon 新闻看板 Web 服务 — 用于 Railway 部署"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import httpx
from dotenv import load_dotenv
from src.models import RSSSourceConfig
from src.scrapers.rss import RSSScraper
from scripts.cookie_sources import fetch_all_cookie_sources
from scripts.xwlb_source import fetch_xwlb
from scripts.ai_analyzer import analyze_xwlb, format_analysis_html

load_dotenv()

# ── 全局缓存 ──
_cached_html: str = ""
_last_update: str = "暂无"
_is_refreshing: bool = False
_refresh_count: int = 0

# ── 源分类 ──
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
NEWS_NAMES = ["新闻联播"]

# ── 北京时间 ──
def _bj_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)

# ── 工具函数 ──
def _title(item): return item["title"] if isinstance(item, dict) else (item.title or "无标题")
def _url(item):   return item["url"]   if isinstance(item, dict) else str(item.url)
def _time(item):
    if isinstance(item, dict): return item.get("time", "")
    return item.published_at.strftime("%m/%d %H:%M") if item.published_at else ""
def _meta(item):  return item.get("meta","") if isinstance(item, dict) else ""
def _content(item): return item.get("content","") if isinstance(item, dict) else (item.content or "")

# ── 翻译 ──
_trans_cache = {}
async def translate(text: str, client: httpx.AsyncClient) -> str:
    if not text or len(text) < 3: return ""
    text = text.strip()
    if text in _trans_cache: return _trans_cache[text]
    try:
        r = await client.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client":"gtx","sl":"en","tl":"zh-CN","dt":"t","q":text[:500]},
            timeout=8,
        )
        result = r.json()[0][0][0]
        _trans_cache[text] = result
        return result
    except: return ""

# ════════════════════════════════════════════
# HTML 生成函数（从 show_news.py 提取）
# ════════════════════════════════════════════

def _format_xwlb_brief(text: str) -> str:
    lines = text.split('\n')
    html_parts = []
    for line in lines:
        line = line.strip()
        if not line: continue
        clean = re.sub(r'\s+', ' ', line)
        if len(clean) < 40 and re.search(r'[。！？]$', clean):
            html_parts.append(f'<div class="xwlb-brief-item">◆ {clean}</div>')
        elif len(clean) < 40 and not re.search(r'[。！？，、；：，]', clean[-1]):
            html_parts.append(f'<div class="xwlb-brief-item">◆ {clean}</div>')
        else:
            html_parts.append(f'<span class="xwlb-text">{line}</span>')
    if not html_parts: return f'<div class="xwlb-text">{text}</div>'
    return '<div class="xwlb-brief-wrap">' + '\n'.join(html_parts) + '</div>'


def render_source_block(source_name, items, color, translations=None, inner_only=False, is_news=False, analysis_html=None):
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
        extra = " item-more" if j >= 10 else ""
        items_html += f"""
        <div class="item{extra}" data-source="{source_name}">
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
            <button class="more-btn" onclick="toggleMore('{source_name}')">展开全部 {len(items)} 条 ▾</button>
        </div>"""
    inner = f"""
        <div class="ch">
            <span>{color['icon']} {source_name}</span>
            <span class="cc">{len(items)}</span>
        </div>
        <div class="ci">{items_html}{more_btn}</div>"""
    if inner_only: return inner
    return f'<div class="col" style="--a:{color["accent"]};--l:{color["light"]};--b:{color["border"]}">{inner}</div>'


def _render_news_block(source_name, items, color, inner_only=False, analysis_html=None):
    left_html = ""
    for j, item in enumerate(items):
        title = _title(item)
        url = _url(item)
        content = _content(item)
        content_clean = re.sub(r'^央视网消息（新闻联播）[：:]\s*', '', content)
        if not content_clean: content_clean = content
        is_brief = '快讯' in title
        formatted_text = _format_xwlb_brief(content_clean) if is_brief and content_clean else f'<div class="xwlb-text">{content_clean}</div>'
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
    if inner_only: return inner
    return f'<div class="col" style="--a:{color["accent"]};--l:{color["light"]};--b:{color["border"]}">{inner}</div>'


def generate_html(domestic, foreign, news, trans, all_items, xwlb_analysis=None):
    now = _bj_now().strftime("%m/%d %H:%M")
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
    all_names = list(domestic.keys()) + list(foreign.keys()) + list(news.keys())
    color_map = {}
    for i, name in enumerate(all_names): color_map[name] = color_list[i % len(color_list)]

    def sidebar_group(title, names, tab_id):
        items_html = ""
        for idx, name in enumerate(names):
            c = color_map.get(name, color_list[0])
            items_html += f"""
            <div class="sl-item" draggable="true" data-name="{name}" data-tab="{tab_id}" data-idx="{idx}"
                 ondragstart="onDragStart(event)" ondragover="onDragOver(event)"
                 ondrop="onDrop(event)" ondragend="onDragEnd(event)">
                <span class="sl-drag">⠿</span>
                <label class="sl-label" onclick="event.stopPropagation()">
                    <input type="checkbox" checked onchange="toggleSource('{name}',this.checked)">
                    <span class="sl-dot" style="background:{c['accent']}"></span>
                    <span class="sl-name">{name}</span>
                </label>
            </div>"""
        icon = {'domestic':'🇨🇳','foreign':'🌍','news':'📺'}.get(tab_id,'📡')
        return f'<div class="sl-group" data-tab="{tab_id}"><div class="sl-group-title">{icon} {title}</div>{items_html}</div>'

    sidebar_html = (
        sidebar_group("国内资讯", list(domestic.keys()), "domestic") +
        sidebar_group("国外资讯", list(foreign.keys()), "foreign") +
        sidebar_group("新闻联播", list(news.keys()), "news")
    )

    source_dat = {}
    for name, items in all_items.items():
        c = color_map.get(name, color_list[0])
        if name in news: tab = "news"
        elif name in domestic: tab = "domestic"
        else: tab = "foreign"
        source_dat[name] = {
            "inner": render_source_block(name, items, c, trans.get(name), inner_only=True,
                                          is_news=(name in NEWS_NAMES), analysis_html=(xwlb_analysis if name in NEWS_NAMES else None)),
            "accent": c['accent'], "light": c['light'], "border": c['border'], "icon": c['icon'],
            "count": len(items), "tab": tab,
        }
    source_data_json = json.dumps(source_dat, ensure_ascii=False)

    d_count = sum(len(v) for v in domestic.values())
    f_count = sum(len(v) for v in foreign.values())
    n_count = sum(len(v) for v in news.values())

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新闻看板</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:#f4f5f7;color:#333;line-height:1.4;display:flex;min-height:100vh}}
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
.main{{flex:1;min-width:0;padding:10px 14px}}
.hd{{display:flex;align-items:baseline;justify-content:center;gap:12px;padding:10px 0 6px;flex-wrap:wrap}}
.hd h1{{font-size:20px;font-weight:700;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.hd .credit{{font-size:11px;color:#bbb}}
.update-bar{{text-align:center;padding:2px 0 4px;font-size:11px;color:#999}}
.stats{{display:flex;gap:14px;margin-bottom:8px;padding:4px 12px;background:#fff;border-radius:8px;font-size:11px;color:#888;justify-content:center}}
.tabs{{display:flex;justify-content:center;gap:6px;margin-bottom:8px}}
.tab-btn{{padding:6px 20px;border:2px solid transparent;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;background:#eee;color:#888;transition:all .15s}}
.tab-btn:hover{{background:#e0e0e0}}
.tab-btn.active{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff}}
.tab-btn .tab-count{{font-size:11px;font-weight:400;opacity:.8;margin-left:4px}}
.tab-content{{display:none}}
.tab-content.active{{display:block}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:8px}}
.col{{background:var(--l);border:1px solid var(--b);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.ci{{padding:3px;flex:1}}
.ch{{display:flex;align-items:center;gap:6px;padding:7px 10px;background:var(--a);color:#fff;font-size:12px;font-weight:600;flex-shrink:0}}
.cc{{margin-left:auto;font-size:10px;opacity:.8;background:rgba(255,255,255,.2);padding:1px 7px;border-radius:7px}}
.hidden-source{{display:none!important}}
.col[data-name="新闻联播"]{{grid-column:1/-1}}
.item{{display:flex;gap:6px;padding:5px 8px;margin:2px 0;background:#fff;border-radius:5px;align-items:flex-start}}
.item:hover{{box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.ir{{flex-shrink:0;width:18px;height:18px;border-radius:3px;background:var(--l);color:var(--a);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-top:1px}}
.ib{{flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:2px 6px;align-items:baseline;text-decoration:none;color:inherit}}
.it{{font-size:13px;font-weight:500;color:#1a1a2e;line-height:1.35;word-break:break-word;width:100%}}
.ib:hover .it{{color:var(--a)}}
.it-trans{{font-size:11px;color:#888;line-height:1.3;width:100%;padding-left:2px}}
.im{{font-size:10px;color:#bbb;white-space:nowrap;flex-shrink:0;margin-top:2px}}
.more-wrap{{text-align:center;padding:6px 0 8px;flex-shrink:0}}
.more-btn{{border:none;background:var(--l);color:var(--a);font-size:11px;cursor:pointer;padding:4px 14px;border-radius:6px;font-weight:500}}
.more-btn:hover{{background:var(--b)}}
.item-more{{display:none}}
.item-more.show{{display:flex}}
.ft{{text-align:center;padding:14px 0 6px;color:#ccc;font-size:11px}}
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
.xwlb-ai-kw{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:4px;background:#e6f7e6;color:#389e0d}}
.xwlb-brief{{border-left-color:#f59e0b!important;background:#fffbeb!important}}
.xwlb-brief .xwlb-title{{color:#d97706}}
.xwlb-brief-wrap{{font-size:13px;line-height:1.7}}
.xwlb-brief-item{{font-weight:700;color:#92400e;padding:4px 0 2px 0;font-size:13px;border-bottom:1px dashed #fde68a;margin:4px 0}}
@media(max-width:900px){{.sidebar{{display:none}}.main{{padding:10px}}.xwlb-layout{{flex-direction:column}}}}
</style>
</head>
<body>
<div class="sidebar" id="sidebar">
    <div class="sidebar-title">▣ 选择信息源</div>
    {sidebar_html}
</div>
<div class="main">
<div class="hd">
    <h1>🌅 新闻看板</h1>
    <span class="credit">感谢 jxtan 和 Thysrael</span>
</div>
<div class="update-bar">🕐 上次更新: {_bj_now().strftime("%H:%M 北京时间")}</div>
<div class="stats">📡 <span>{len(all_items)}</span> 源 · 📰 <span>{total}</span> 条</div>
<div class="tabs">
    <button class="tab-btn active" onclick="switchTab('domestic',this)">🇨🇳 国内 <span class="tab-count" id="cnt-domestic">{d_count}</span></button>
    <button class="tab-btn" onclick="switchTab('foreign',this)">🌍 国外 <span class="tab-count" id="cnt-foreign">{f_count}</span></button>
    <button class="tab-btn" onclick="switchTab('news',this)">📺 新闻联播 <span class="tab-count" id="cnt-news">{n_count}</span></button>
</div>
<div id="tab-domestic" class="tab-content active"><div class="grid" id="grid-domestic"></div></div>
<div id="tab-foreign" class="tab-content"><div class="grid" id="grid-foreign"></div></div>
<div id="tab-news" class="tab-content"><div class="grid" id="grid-news"></div></div>
<div class="ft">Powered by <strong>Horizon</strong></div>
</div>
<script>
var SOURCE_DATA = {source_data_json};
var STATE_KEY = "horizon_state"; var ORDER_KEY = "horizon_order"; var currentTab = "domestic";
function loadState(){{try{{return JSON.parse(localStorage.getItem(STATE_KEY))||{{}}}}catch(e){{return{{}}}}}}
function saveState(s){{try{{localStorage.setItem(STATE_KEY,JSON.stringify(s))}}catch(e){{}}}}
function loadOrder(){{try{{return JSON.parse(localStorage.getItem(ORDER_KEY))}}catch(e){{return null}}}}
function saveOrder(o){{try{{localStorage.setItem(ORDER_KEY,JSON.stringify(o))}}catch(e){{}}}}
var dragSrc=null;
function onDragStart(e){{dragSrc=e.currentTarget;e.currentTarget.classList.add("dragging")}}
function onDragOver(e){{e.preventDefault();if(e.currentTarget!==dragSrc&&e.currentTarget.dataset.tab===dragSrc.dataset.tab)e.currentTarget.classList.add("drag-over")}}
function onDrop(e){{e.preventDefault();e.currentTarget.classList.remove("drag-over");if(dragSrc&&dragSrc.dataset.tab===e.currentTarget.dataset.tab){{var p=dragSrc.parentNode;var a=Array.from(p.querySelectorAll(".sl-item"));var s=a.indexOf(dragSrc);var d=a.indexOf(e.currentTarget);if(s<d)p.insertBefore(dragSrc,e.currentTarget.nextSibling);else p.insertBefore(dragSrc,e.currentTarget);persistOrder();renderGrid(currentTab)}}dragSrc=null}}
function onDragEnd(e){{e.currentTarget.classList.remove("dragging");document.querySelectorAll(".drag-over").forEach(function(el){{el.classList.remove("drag-over")}})}}
function persistOrder(){{var o=[];document.querySelectorAll(".sl-item").forEach(function(el){{o.push(el.dataset.name)}});saveOrder(o)}}
function restoreSidebarOrder(){{var o=loadOrder();if(!o)return;document.querySelectorAll(".sl-group").forEach(function(g){{var a=Array.from(g.querySelectorAll(".sl-item"));a.sort(function(a,b){{return o.indexOf(a.dataset.name)-o.indexOf(b.dataset.name)}});a.forEach(function(el){{g.appendChild(el)}})}})}}
function toggleSource(name,vis){{var s=loadState();s[name]=vis;saveState(s);var c=document.querySelector('.col[data-name="'+name+'"]');if(c)c.classList.toggle("hidden-source",!vis);updateCounts()}}
function restoreState(){{var s=loadState();document.querySelectorAll(".sl-item").forEach(function(el){{var n=el.dataset.name;var cb=el.querySelector("input[type=checkbox]");cb.checked=s[n]!==false}})}}
function renderGrid(tab){{var g=document.getElementById("grid-"+tab);var s=loadState();var n=[];document.querySelectorAll('.sl-item[data-tab="'+tab+'"]').forEach(function(el){{n.push(el.dataset.name)}});var h="";n.forEach(function(name){{var d=SOURCE_DATA[name];if(!d)return;var v=s[name]!==false;h+='<div class="col'+(v?"":" hidden-source")+'" data-name="'+name+'" style="--a:'+d.accent+";--l:"+d.light+";--b:"+d.border+'">'+d.inner+"</div>"}});g.innerHTML=h;updateCounts()}}
function updateCounts(){{var s=loadState();var c={{domestic:0,foreign:0,news:0}};Object.keys(SOURCE_DATA).forEach(function(name){{if(s[name]===false)return;var d=SOURCE_DATA[name];if(c[d.tab]!==undefined)c[d.tab]+=d.count}});document.getElementById("cnt-domestic").textContent=c.domestic;document.getElementById("cnt-foreign").textContent=c.foreign;document.getElementById("cnt-news").textContent=c.news}}
function switchTab(t,btn){{currentTab=t;document.querySelectorAll(".tab-btn").forEach(function(b){{b.classList.remove("active")}});document.querySelectorAll(".tab-content").forEach(function(c){{c.classList.remove("active")}});btn.classList.add("active");document.getElementById("tab-"+t).classList.add("active");renderGrid(t)}}
function toggleMore(name){{var items=document.querySelectorAll('.item[data-source="'+name+'"]');var btns=document.querySelectorAll(".more-btn");var btn=null;btns.forEach(function(b){{if(b.getAttribute("onclick")&&b.getAttribute("onclick").includes(name))btn=b}});var hidden=false;items.forEach(function(it){{if(it.classList.contains("item-more")){{it.classList.toggle("show");if(it.classList.contains("show"))hidden=true}}}});if(btn)btn.textContent=hidden?"收起 ▴":"展开全部 "+items.length+" 条 ▾"}}
window.onload=function(){{restoreSidebarOrder();restoreState();renderGrid("domestic")}};
</script>
</body>
</html>'''


# ── 数据刷新 ──
async def refresh_data():
    global _cached_html, _last_update, _is_refreshing, _refresh_count
    if _is_refreshing: return
    _is_refreshing = True
    try:
        print(f"\n🔄 刷新 #{_refresh_count + 1} - {datetime.now().strftime('%H:%M:%S')}")
        all_grouped = {}
        translated = {}
        xwlb_analysis = None

        rss_sources = [
            RSSSourceConfig(name="Hacker News", url="https://hnrss.org/frontpage"),
            RSSSourceConfig(name="Simon Willison", url="https://simonwillison.net/atom/everything/"),
            RSSSourceConfig(name="LWN.net", url="https://lwn.net/headlines/rss"),
            RSSSourceConfig(name="Schneier Security", url="https://www.schneier.com/feed/atom/"),
            RSSSourceConfig(name="CSS-Tricks", url="https://css-tricks.com/feed/"),
            RSSSourceConfig(name="Hackaday", url="https://hackaday.com/feed/"),
            RSSSourceConfig(name="Nature", url="https://www.nature.com/nature.rss"),
            RSSSourceConfig(name="Quanta Magazine", url="https://api.quantamagazine.org/feed/"),
            RSSSourceConfig(name="Ars Technica", url="https://feeds.arstechnica.com/arstechnica/index"),
            RSSSourceConfig(name="The Verge", url="https://www.theverge.com/rss/index.xml"),
            RSSSourceConfig(name="量子位", url="https://wechat2rss.xlab.app/feed/7131b577c61365cb47e81000738c10d872685908.xml"),
            RSSSourceConfig(name="新智元", url="https://wechat2rss.xlab.app/feed/ede30346413ea70dbef5d485ea5cbb95cca446e7.xml"),
            RSSSourceConfig(name="V2EX 热门", url="https://rsshub.rssforever.com/v2ex/topics/hot"),
            RSSSourceConfig(name="少数派", url="https://rsshub.rssforever.com/sspai/index"),
            RSSSourceConfig(name="联合早报", url="https://rsshub.rssforever.com/zaobao/realtime/china"),
            RSSSourceConfig(name="联合早报·国际", url="https://rsshub.rssforever.com/zaobao/realtime/world"),
            RSSSourceConfig(name="澎湃新闻", url="https://rsshub.rssforever.com/thepaper/featured"),
            RSSSourceConfig(name="Linux DO", url="https://linux.do/latest.rss"),
            RSSSourceConfig(name="NodeSeek", url="https://rss.nodeseek.com"),
            RSSSourceConfig(name="知乎热榜", url="https://rsshub.rssforever.com/zhihu/hot"),
        ]

        since = datetime.now(timezone.utc) - timedelta(days=7)
        async with httpx.AsyncClient(timeout=30.0) as client:
            scraper = RSSScraper(rss_sources, client)
            for source in rss_sources:
                try:
                    items = await scraper._fetch_feed(source, since)
                    items.sort(key=lambda x: x.published_at or datetime.min, reverse=True)
                    if items: all_grouped[source.name] = items
                except: pass

            cookie_results = await fetch_all_cookie_sources(client)
            for name, items in cookie_results.items(): all_grouped[name] = items

            try:
                xr = await fetch_xwlb(client=client)
                if xr.get("items") and xr["success"] > 0:
                    items = []
                    for it in xr["items"]:
                        c = it.get("content","") or ""
                        items.append({"title":it["title"],"url":it["url"],"time":"","meta":c.replace('\n',' ').strip()[:120],"content":c})
                    all_grouped["新闻联播"] = items
                    run_key = _bj_now().strftime("%Y%m%d")
                    result = analyze_xwlb(items, run_key=run_key)
                    if result and "error" not in result:
                        print(f"   ✅ AI 分析完成")
                    xwlb_analysis = format_analysis_html(result)
            except Exception as e: print(f"   📺 新闻联播: {e}")

        foreign_set = set(FOREIGN_NAMES)
        f_items = [(n, it) for n, its in all_grouped.items() if n in foreign_set for it in its]
        if f_items:
            async with httpx.AsyncClient(timeout=10.0) as tc:
                batch = [(n, it, _title(it)) for n, it in f_items if _title(it)]
                tasks = [translate(t, tc) for _, _, t in batch]
                results = await asyncio.gather(*tasks)
                for (n, it, t), r in zip(batch, results):
                    if r: translated.setdefault(n, {})[t] = r

        domestic = {}
        foreign = {}
        news = {}
        for n in DOMESTIC_NAMES:
            if n in all_grouped: domestic[n] = all_grouped[n]
        for n in FOREIGN_NAMES:
            if n in all_grouped: foreign[n] = all_grouped[n]
        for n in NEWS_NAMES:
            if n in all_grouped: news[n] = all_grouped[n]

        _cached_html = generate_html(domestic, foreign, news, translated, all_grouped, xwlb_analysis)
        _last_update = _bj_now().strftime("%H:%M 北京时间")
        _refresh_count += 1
        print(f"✅ 刷新完成 - {sum(len(v) for v in all_grouped.values())} 条, {len(all_grouped)} 个源")
    except Exception as e:
        import traceback; traceback.print_exc()
    finally:
        _is_refreshing = False


# ── FastAPI ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 启动中，首次刷新...")
    await refresh_data()
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()

async def _scheduler():
    while True:
        await asyncio.sleep(3600)  # 1 小时刷新一次
        # 北京时间 23:00~07:00 不刷新（休息时段）
        now_bj = _bj_now()
        if now_bj.hour >= 23 or now_bj.hour < 7:
            print(f"⏰ {now_bj.strftime('%H:%M')} 北京深夜时段，跳过刷新")
            continue
        try: await refresh_data()
        except: pass

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def root():
    if not _cached_html: return "<h1>正在刷新数据，请稍后...</h1>"
    return _cached_html

@app.get("/health")
async def health():
    return {"status":"ok","last_update":_last_update,"refresh_count":_refresh_count,"html_size":len(_cached_html)}

@app.post("/refresh")
async def manual_refresh():
    if _is_refreshing: return {"status":"refreshing"}
    asyncio.create_task(refresh_data())
    return {"status":"started"}
