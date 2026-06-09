#!/usr/bin/env python3
"""测试 hotpush-main 项目中的新闻源"""

import asyncio
import sys
import webbrowser
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import httpx
import feedparser


async def fetch_rss(url: str, name: str, client: httpx.AsyncClient) -> list:
    """抓取 RSS 源，返回 (title, url, time) 列表"""
    try:
        response = await client.get(url, follow_redirects=True, timeout=30.0)
        response.raise_for_status()
        feed = feedparser.parse(response.text)
        if not feed.entries:
            return []
        items = []
        for entry in feed.entries[:10]:
            pub = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                import calendar
                pub = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                import calendar
                pub = datetime.fromtimestamp(calendar.timegm(entry.updated_parsed), tz=timezone.utc)
            items.append({
                "title": entry.get('title', '无标题'),
                "url": entry.get('link', ''),
                "time": pub.strftime("%m/%d %H:%M") if pub else "",
                "name": name,
            })
        return items
    except Exception as e:
        return []


def generate_html(grouped_items: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%m/%d %H:%M")

    color_list = [
        {"accent": "#e74c3c", "light": "#fdf2f2", "border": "#f5c6c6", "icon": "🔶"},
        {"accent": "#7c3aed", "light": "#f5f3ff", "border": "#d5ccff", "icon": "🟣"},
        {"accent": "#059669", "light": "#ecfdf5", "border": "#b8e6d3", "icon": "🟢"},
        {"accent": "#0284c7", "light": "#f0f9ff", "border": "#b8dff5", "icon": "🔵"},
        {"accent": "#d97706", "light": "#fffbeb", "border": "#fdebb3", "icon": "🟡"},
        {"accent": "#dc2626", "light": "#fef2f2", "border": "#f5c2c2", "icon": "🔴"},
        {"accent": "#0891b2", "light": "#ecfeff", "border": "#a5f3fc", "icon": "🩵"},
        {"accent": "#9333ea", "light": "#faf5ff", "border": "#e1ccff", "icon": "🟣"},
        {"accent": "#ea580c", "light": "#fff7ed", "border": "#fed7aa", "icon": "🟠"},
        {"accent": "#2563eb", "light": "#eff6ff", "border": "#bfdbfe", "icon": "🔷"},
        {"accent": "#65a30d", "light": "#f7fee7", "border": "#d9f99d", "icon": "💚"},
        {"accent": "#db2777", "light": "#fdf2f8", "border": "#fbcfe8", "icon": "💗"},
        {"accent": "#0d9488", "light": "#f0fdfa", "border": "#99f6e4", "icon": "💎"},
        {"accent": "#4f46e5", "light": "#eef2ff", "border": "#c7d2fe", "icon": "💙"},
    ]

    columns_html = ""
    total_items = 0
    for idx, (source_name, items) in enumerate(grouped_items.items()):
        total_items += len(items)
        c = color_list[idx % len(color_list)]

        items_html = ""
        for j, item in enumerate(items):
            title = item["title"]
            url = item["url"]
            pub_time = item["time"]
            items_html += f"""
            <div class="item">
                <span class="ir">{j+1}</span>
                <a class="ib" href="{url}" target="_blank" rel="noopener">
                    <span class="it">{title}</span>
                    <span class="im">{pub_time}</span>
                </a>
            </div>"""

        columns_html += f"""
        <div class="col" style="--a:{c['accent']};--l:{c['light']};--b:{c['border']}">
            <div class="ch">
                <span>{c['icon']} {source_name}</span>
                <span class="cc">{len(items)}</span>
            </div>
            <div class="ci">{items_html}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HotPush 新闻源测试</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;background:#f4f5f7;color:#333;line-height:1.4}}
.c{{max-width:100%;margin:0 auto;padding:10px}}
.hd{{text-align:center;padding:14px 0 8px;margin-bottom:10px}}
.hd h1{{font-size:20px;font-weight:700;background:linear-gradient(135deg,#667eea,#764ba2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;display:inline}}
.hd p{{color:#aaa;font-size:11px;display:inline;margin-left:12px}}
.stats{{display:flex;gap:16px;margin-bottom:10px;padding:6px 12px;background:#fff;border-radius:8px;font-size:12px;color:#888;justify-content:center}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:8px;align-items:start}}
.col{{background:var(--l);border:1px solid var(--b);border-radius:8px;overflow:hidden}}
.ch{{display:flex;align-items:center;gap:6px;padding:7px 10px;background:var(--a);color:#fff;font-size:12px;font-weight:600}}
.cc{{margin-left:auto;font-size:10px;opacity:.8;background:rgba(255,255,255,.2);padding:1px 7px;border-radius:7px}}
.ci{{padding:3px}}
.item{{display:flex;gap:6px;padding:5px 8px;margin:2px 0;background:#fff;border-radius:5px;align-items:flex-start}}
.item:hover{{box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.ir{{flex-shrink:0;width:18px;height:18px;border-radius:3px;background:var(--l);color:var(--a);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-top:1px}}
.ib{{flex:1;min-width:0;display:flex;gap:6px;align-items:baseline;text-decoration:none;color:inherit}}
.it{{font-size:13px;font-weight:500;color:#1a1a2e;line-height:1.35;word-break:break-word}}
.ib:hover .it{{color:var(--a)}}
.im{{font-size:10px;color:#bbb;white-space:nowrap;flex-shrink:0;margin-top:2px}}
.ft{{text-align:center;padding:14px 0 6px;color:#ccc;font-size:11px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="c">
<div class="hd"><h1>🔥 HotPush 新闻源测试</h1><p>更新 {now}</p></div>
<div class="stats">📡 <span>{len(grouped_items)}</span> 源 · 📰 <span>{total_items}</span> 条</div>
<div class="grid">{columns_html}</div>
<div class="ft">来自 hotpush-main 项目 · RSSHub 中转</div>
</div>
</body>
</html>"""
    return html


async def main():
    Rsshub = "https://rsshub.rssforever.com"

    # 从 hotpush-main 拷过来的源（RSSHub 路由）
    sources = [
        # 直接 RSS URL
        ("Linux DO",     "https://linux.do/latest.rss",          "direct"),
        ("NodeSeek",     "https://rss.nodeseek.com",             "direct"),
        # RSSHub 路由
        ("微博热搜",     f"{Rsshub}/weibo/search/hot",          "rsshub"),
        ("知乎热榜",     f"{Rsshub}/zhihu/hot",                 "rsshub"),
        ("B站热搜",      f"{Rsshub}/bilibili/hot-search",       "rsshub"),
        ("V2EX 热门",    f"{Rsshub}/v2ex/topics/hot",           "rsshub"),
        ("Hacker News",  f"{Rsshub}/hackernews/best",           "rsshub"),
        ("掘金热榜",     f"{Rsshub}/juejin/trending/all/weekly","rsshub"),
        ("少数派",       f"{Rsshub}/sspai/index",                "rsshub"),
        ("豆瓣热映",     f"{Rsshub}/douban/movie/playing",      "rsshub"),
        ("豆瓣新书",     f"{Rsshub}/douban/book/latest",        "rsshub"),
        ("联合早报",     f"{Rsshub}/zaobao/realtime/china",     "rsshub"),
        ("澎湃新闻",     f"{Rsshub}/thepaper/featured",         "rsshub"),
    ]

    print(f"\n🔥 HotPush 新闻源测试")
    print(f"{'='*55}")
    print(f"📡 RSSHub 实例: {Rsshub}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        grouped_items = defaultdict(list)

        for name, url, stype in sources:
            tag = "🔗" if stype == "direct" else "📡"
            print(f"   {tag} {name:12s} ... ", end="", flush=True)
            items = await fetch_rss(url, name, client)
            if items:
                print(f"✅ {len(items)} 条")
                grouped_items[name] = items
            else:
                print(f"❌ 失败")

    if not grouped_items:
        print("\n❌ 全部失败")
        return

    total = sum(len(v) for v in grouped_items.values())
    print(f"\n   ✅ 成功: {len(grouped_items)}/{len(sources)} 个源, {total} 条")
    for n, v in grouped_items.items():
        print(f"      • {n}: {len(v)} 条")

    html_content = generate_html(dict(grouped_items))
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)
    html_path = output_dir / "hotpush_test.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"\n   💾 {html_path}")

    file_url = f"file://{html_path.resolve()}"
    print(f"   🚀 打开浏览器...")
    webbrowser.open(file_url)
    print(f"\n{'='*55}")
    print(f"✅ 完成\n")


if __name__ == "__main__":
    asyncio.run(main())
