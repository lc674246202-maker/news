#!/usr/bin/env python3
"""新闻联播文字稿抓取 — 按天缓存（每天只抓一次）"""

import asyncio
import json
import re
import html as html_module
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

# ── 文件缓存目录 ──
_CACHE_DIR = Path(__file__).parent.parent / "data" / "xwlb_cache"
_XWLB_CACHE: dict = {}
_XWLB_CACHE_KEY: Optional[str] = None


def _cache_path(run_date_key: str) -> Path:
    """获取当天缓存路径（按运行日期，不是新闻日期）"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"xwlb_{run_date_key}.json"


def _load_cache(run_date_key: str) -> Optional[dict]:
    """读取当天缓存"""
    path = _cache_path(run_date_key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(run_date_key: str, data: dict):
    """保存当天缓存"""
    path = _cache_path(run_date_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def fetch_xwlb(date: datetime = None, client: httpx.AsyncClient = None) -> dict:
    """
    抓取新闻联播（每天只抓一次，文件缓存）。
    总是取昨天的新闻（央视联播19:00播出）。
    缓存键 = 运行日期（今天），同一天再运行直接读文件。

    返回: {"date":"2026年06月08日","items":[...], "total":9, "success":9, "failed":0}
    """
    global _XWLB_CACHE, _XWLB_CACHE_KEY

    # 运行日期（今天）
    now = datetime.now(timezone.utc)
    run_key = now.strftime("%Y%m%d")

    # 目标新闻日期（昨天）
    d = date or (now - timedelta(days=1))
    target_str = d.strftime("%Y%m%d")
    date_str = f"{d.year}年{d.month:02d}月{d.day:02d}日"

    # 进程内缓存
    if _XWLB_CACHE_KEY == run_key and _XWLB_CACHE:
        return _XWLB_CACHE

    # 文件缓存
    cached = _load_cache(run_key)
    if cached:
        _XWLB_CACHE = cached
        _XWLB_CACHE_KEY = run_key
        return cached

    # ── 没有缓存，开始抓取 ──
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/125.0.0.0 Safari/537.36",
    }

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers)
        close_client = True

    try:
        # 第一步：获取新闻列表页
        list_url = f"https://tv.cctv.com/lm/xwlb/day/{target_str}.shtml"
        resp = await client.get(list_url)
        resp.raise_for_status()
        html = resp.text

        # 解析新闻列表
        pattern = re.compile(
            r'<a href="(https://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/VIDE[^"]+)"[^>]*title="([^"]+)"'
        )
        matches = pattern.findall(html)

        seen = set()
        news_items = []
        for url, title in matches:
            title = title.strip()
            title = re.sub(r'^\[视频\]\s*', '', title)
            if not title or len(title) < 3 or '完整版' in title or title.startswith('《新闻联播》'):
                continue
            key = title[:20]
            if key not in seen:
                seen.add(key)
                news_items.append({"title": title, "url": url})

        if not news_items:
            return {"date": date_str, "items": [], "error": "未找到新闻列表"}

        # 第二步：并发获取每条新闻详情
        sem = asyncio.Semaphore(5)

        async def fetch_detail(item: dict) -> dict:
            async with sem:
                try:
                    await asyncio.sleep(0.3)
                    r = await client.get(item["url"])
                    r.raise_for_status()
                    content = _extract_content(r.text)
                    return {**item, "content": content}
                except Exception:
                    return {**item, "content": None}

        tasks = [fetch_detail(item) for item in news_items]
        results = await asyncio.gather(*tasks)

        success = sum(1 for r in results if r.get("content"))
        failed = sum(1 for r in results if not r.get("content"))

        result = {
            "date": date_str,
            "items": results,
            "total": len(results),
            "success": success,
            "failed": failed,
        }

        # 写入文件缓存
        _save_cache(run_key, result)
        _XWLB_CACHE = result
        _XWLB_CACHE_KEY = run_key

        return result

    finally:
        if close_client and client is not None:
            await client.aclose()


def _extract_content(html: str) -> Optional[str]:
    """从详情页 HTML 提取正文"""
    start_match = re.search(r'<div class="content_area"[^>]*>', html)
    if not start_match:
        return None
    start_pos = start_match.end()
    end_match = re.search(r'<div class="zebian">', html[start_pos:])
    if end_match:
        content = html[start_pos:start_pos + end_match.start()]
    else:
        content = html[start_pos:start_pos + 8000]
    content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
    content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
    content = re.sub(r'<br\s*/?>', '\n', content)
    content = re.sub(r'</p>', '\n', content)
    content = re.sub(r'<[^>]+>', '', content)
    content = html_module.unescape(content)
    content = re.sub(r'&nbsp;', ' ', content)
    content = re.sub(r'&[ld]dquo;', '"', content)
    content = re.sub(r'&mdash;', '——', content)
    content = re.sub(r'&middot;', '·', content)
    content = re.sub(r'\n\s*\n+', '\n', content)
    content = re.sub(r'[ \t]+', ' ', content)
    return content.strip() if content.strip() else None


async def test():
    d = datetime.now(timezone.utc) - timedelta(days=1)
    result = await fetch_xwlb(d)
    print(f"📺 新闻联播 — {result['date']}")
    print(f"   共 {result['total']} 条, 成功: {result['success']}, 失败: {result['failed']}")
    for i, item in enumerate(result["items"][:5], 1):
        content_preview = re.sub(r'\s+', ' ', item.get("content", "") or "").strip()[:80]
        print(f"  {i}. {item['title']}")
        if content_preview:
            print(f"     摘要: {content_preview}...")


if __name__ == "__main__":
    asyncio.run(test())
