#!/usr/bin/env python3
"""Horizon RSS 抓取测试脚本 - 不依赖 AI 包"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 将项目根目录加入 Python 路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import httpx
from src.models import ContentItem, SourceType, RSSSourceConfig
from src.scrapers.rss import RSSScraper


async def test_rss_feeds():
    """测试 RSS 抓取功能"""

    # 配置 3 个测试用的 RSS 源
    test_sources = [
        RSSSourceConfig(
            name="阮一峰网络日志",
            url="https://www.ruanyifeng.com/blog/atom.xml",
            enabled=True,
            category="tech",
        ),
        RSSSourceConfig(
            name="Hacker News",
            url="https://hnrss.org/frontpage",
            enabled=True,
            category="tech",
        ),
        RSSSourceConfig(
            name="Simon Willison",
            url="https://simonwillison.net/atom/everything/",
            enabled=True,
            category="ai-tools",
        ),
    ]

    # 时间窗口：最近 7 天
    since = datetime.now(timezone.utc) - timedelta(days=7)

    print(f"📡 Horizon RSS 抓取测试")
    print(f"{'='*60}")
    print(f"时间窗口: 最近 7 天（从 {since.strftime('%Y-%m-%d %H:%M')} 起）")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=30.0) as client:
        scraper = RSSScraper(test_sources, client)

        for source in test_sources:
            print(f"🔍 正在抓取: {source.name}")
            print(f"   URL: {source.url}")
            try:
                items = await scraper._fetch_feed(source, since)
                if items:
                    print(f"   ✅ 成功获取 {len(items)} 条内容:\n")
                    for i, item in enumerate(items[:5], 1):  # 每个源最多展示 5 条
                        print(f"   [{i}] {item.title}")
                        print(f"       源: {item.metadata.get('feed_name', '未知')}")
                        print(f"       时间: {item.published_at.strftime('%Y-%m-%d %H:%M')}")
                        print(f"       链接: {item.url}")
                        if item.content:
                            content_preview = item.content[:120].replace("\n", " ")
                            print(f"       预览: {content_preview}...")
                        print()
                else:
                    print(f"   ℹ️  该源在时间窗口内没有新内容\n")
            except Exception as e:
                print(f"   ❌ 抓取失败: {e}\n")

    print(f"{'='*60}")
    print("✅ RSS 抓取测试完成")


if __name__ == "__main__":
    asyncio.run(test_rss_feeds())
