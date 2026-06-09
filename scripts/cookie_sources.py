#!/usr/bin/env python3
"""Cookie 直连爬虫：知乎热榜、微博热搜、B站热搜"""

import json
import re
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import httpx


# ============================================================
# cookie 存储路径：data/cookies.json
# 格式：
# {
#   "zhihu": "xxx",
#   "weibo": "xxx",
#   "bilibili": "xxx"
# }
# ============================================================

COOKIE_FILE = Path(__file__).parent.parent / "data" / "cookies.json"


def load_cookies() -> dict:
    """从 cookies.json 加载 cookie"""
    if not COOKIE_FILE.exists():
        return {}
    try:
        with open(COOKIE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


# ──────────────────────────────────────────────
# 1. 知乎热榜
# ──────────────────────────────────────────────

ZHIHU_HOT_URL = "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50"

ZHIHU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.zhihu.com/hot",
    "Origin": "https://www.zhihu.com",
}


def _zhihu_x_xx_token(cookie: str) -> str:
    """从 zhihu cookie 中提取 x-xx-token（从 _xsrf 或 -R- 字段）"""
    m = re.search(r"_xsrf=([^;]+)", cookie)
    if m:
        return m.group(1)
    m = re.search(r"-R-=([^;]+)", cookie)
    if m:
        return m.group(1)
    return ""


async def fetch_zhihu(cookie: str, client: httpx.AsyncClient) -> list:
    """抓取知乎热榜"""
    if not cookie:
        return []

    headers = {**ZHIHU_HEADERS, "Cookie": cookie}
    token = _zhihu_x_xx_token(cookie)
    if token:
        headers["x-xx-token"] = token

    try:
        # 先访问首页拿 cookie（补充 d_c0 等）
        try:
            await client.get("https://www.zhihu.com/hot", headers=headers, follow_redirects=True, timeout=15)
        except Exception:
            pass

        time.sleep(random.uniform(0.5, 1.0))

        resp = await client.get(ZHIHU_HOT_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        items = []
        for entry in data.get("data", []):
            target = entry.get("target", {})
            question = target.get("question", {})

            title = target.get("title", "") or question.get("title", "")
            url = target.get("url", "") or f"https://www.zhihu.com/question/{question.get('id', '')}"
            # 知乎的 url 可能是 /question/xxx 格式，补全
            if url.startswith("/"):
                url = "https://www.zhihu.com" + url
            detail_text = entry.get("detail_text", "")
            excerpt = (target.get("excerpt", "") or question.get("excerpt", "") or "")[:100]

            items.append({
                "title": title.strip(),
                "url": url,
                "time": "",
                "meta": detail_text,
                "summary": excerpt,
            })
        return items
    except Exception:
        return []


# ──────────────────────────────────────────────
# 2. 微博热搜
# ──────────────────────────────────────────────

WEIBO_HOT_URL = "https://weibo.com/ajax/side/hotSearch"

WEIBO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://weibo.com/",
}


async def fetch_weibo(cookie: str, client: httpx.AsyncClient) -> list:
    """抓取微博热搜"""
    if not cookie:
        return []

    headers = {**WEIBO_HEADERS, "Cookie": cookie}

    try:
        resp = await client.get(WEIBO_HOT_URL, headers=headers, follow_redirects=True, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()

        items = []
        # 微博热搜返回 realtime 数组
        for item in data.get("data", {}).get("realtime", []):
            word = item.get("word", "").strip()
            if not word:
                continue
            # 热搜链接
            is_ad = item.get("is_ad", False)
            raw_url = item.get("url", "")
            if raw_url.startswith("//"):
                raw_url = "https:" + raw_url
            if not raw_url:
                raw_url = f"https://s.weibo.com/weibo?q={word}"
            num = item.get("raw_hot", 0) or item.get("rank", 0)

            items.append({
                "title": word,
                "url": raw_url,
                "time": "",
                "meta": f"热度 {num}" if num else "",
                "summary": "",
            })
        return items
    except Exception:
        return []


# ──────────────────────────────────────────────
# 3. B站热搜
# ──────────────────────────────────────────────

BILIBILI_HOT_URL = "https://api.bilibili.com/x/web-interface/search/square?limit=50"
BILIBILI_RANK_URL = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"

BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.bilibili.com/",
}


async def fetch_bilibili(cookie: str, client: httpx.AsyncClient) -> list:
    """抓取 B站热搜"""
    headers = {**BILIBILI_HEADERS}
    if cookie:
        headers["Cookie"] = cookie

    items = []

    # 方式一：热搜词
    try:
        resp = await client.get(BILIBILI_HOT_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", {}).get("trending", {}).get("list", []):
            keyword = item.get("keyword", "").strip()
            if not keyword:
                continue
            items.append({
                "title": keyword,
                "url": f"https://search.bilibili.com/all?keyword={keyword}",
                "time": "",
                "meta": f"热度 {item.get('hot_id', '')}" if item.get('hot_id') else "",
                "summary": item.get("show_name", "") or "",
            })
    except Exception:
        pass

    # 方式二：视频排行榜（作为补充）
    try:
        resp = await client.get(BILIBILI_RANK_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for i, v in enumerate(data.get("data", {}).get("list", [])[:10]):
            title = v.get("title", "").strip()
            if not title:
                continue
            # B站标题可能有 emoji 标签如 "UP主xxx"，清理
            title_clean = re.sub(r'<[^>]+>', '', title)
            items.append({
                "title": f"[排行] {title_clean}",
                "url": f"https://www.bilibili.com/video/{v.get('bvid', '')}",
                "time": "",
                "meta": f"播放 {v.get('stat', {}).get('view', 0)}",
                "summary": v.get("desc", "")[:80] or "",
            })
    except Exception:
        pass

    return items


# ──────────────────────────────────────────────
# 统一接口
# ──────────────────────────────────────────────

async def fetch_all_cookie_sources(client: httpx.AsyncClient) -> dict:
    """抓取所有需要 cookie 的源，返回 {源名称: [items]}"""
    cookies = load_cookies()

    results = {}

    zhihu_cookie = cookies.get("zhihu", "")
    if zhihu_cookie:
        items = await fetch_zhihu(zhihu_cookie, client)
        if items:
            results["知乎热榜"] = items

    weibo_cookie = cookies.get("weibo", "")
    if weibo_cookie:
        items = await fetch_weibo(weibo_cookie, client)
        if items:
            results["微博热搜"] = items

    bilibili_cookie = cookies.get("bilibili", "")
    items = await fetch_bilibili(bilibili_cookie, client)
    if items:
        results["B站热搜"] = items

    return results
