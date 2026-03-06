import os
import yaml
import json
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz


class AfterhoursCollector:
    """장후 뉴스 수집기 v4.0 (16:30 ~ 09:00 EST, 30분마다)"""

    def __init__(self):
        print("🌙 Afterhours Collector v4.0 초기화...")

        self.news_api_key = os.environ['NEWS_API_KEY']
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        self.afterhours_file = 'afterhours_news.json'
        self.seen_file = 'seen_afterhours.json'

        # 추적 키워드 (자산 관련)
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        self.keywords = set()
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in config.get(section, []):
                for kw in asset.get('keywords', []):
                    self.keywords.add(kw.lower())

        # 기본 키워드 추가
        self.keywords.update([
            'fed', 'interest rate', 'inflation', 'gdp', 'recession',
            'earnings', 'revenue', 'profit', 'tariff', 'trade war',
            'nasdaq', 's&p', 'dow', 'market', 'stock', 'economy',
            'bank', 'oil', 'gold', 'crypto', 'bitcoin'
        ])

        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"🔑 추적 키워드: {len(self.keywords)}개")

    # --------------------------------------------------------
    # seen_afterhours 관리
    # --------------------------------------------------------
    def load_seen_ids(self):
        try:
            with open(self.seen_file, 'r') as f:
                data = json.load(f)
            # 오늘/어제 날짜만 유지 (장후는 날짜 넘김)
            return set(data.get('seen_ids', []))
        except:
            return set()

    def save_seen_ids(self, seen_ids):
        # 최대 1000개만 유지
        seen_list = list(seen_ids)[-1000:]
        with open(self.seen_file, 'w') as f:
            json.dump({'seen_ids': seen_list, 'updated': self.now.isoformat()}, f)

    # --------------------------------------------------------
    # afterhours_news.json 로드/저장
    # --------------------------------------------------------
    def load_afterhours_news(self):
        try:
            with open(self.afterhours_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # 오늘 날짜가 아니면 초기화 (아침 브리핑이 초기화 안 했을 때 대비)
            if data.get('date') != self.now.strftime('%Y-%m-%d'):
                return []
            return data.get('news', [])
        except:
            return []

    def save_afterhours_news(self, news_list):
        with open(self.afterhours_file, 'w', encoding='utf-8') as f:
            json.dump({
                'date': self.now.strftime('%Y-%m-%d'),
                'updated': self.now.isoformat(),
                'count': len(news_list),
                'news': news_list
            }, f, ensure_ascii=False, indent=2)
        print(f"💾 afterhours_news.json 저장 ({len(news_list)}개)")

    # --------------------------------------------------------
    # 뉴스 수집
    # --------------------------------------------------------
    def collect_news(self):
        print("\n📰 장후 뉴스 수집 중...")
        all_news = []
        seen_urls = set()

        # 1. NewsAPI
        for category in ['business', 'technology']:
            try:
                response = requests.get(
                    "https://newsapi.org/v2/top-headlines",
                    params={
                        'language': 'en',
                        'apiKey': self.news_api_key,
                        'pageSize': 100,
                        'category': category,
                        'country': 'us'
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    for a in response.json().get('articles', []):
                        url = a.get('url', '')
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_news.append({
                                'title': a.get('title', ''),
                                'description': a.get('description', ''),
                                'url': url,
                                'source': a.get('source', {}).get('name', ''),
                                'published': a.get('publishedAt', ''),
                                'content': '',
                                'collected_at': self.now.isoformat()
                            })
                print(f"   NewsAPI {category}: {len(all_news)}개")
            except Exception as e:
                print(f"   ❌ NewsAPI {category}: {e}")

        # 2. RSS 피드
        rss_feeds = [
            ('Reuters Business', 'https://feeds.reuters.com/reuters/businessNews'),
            ('Reuters Finance', 'https://feeds.reuters.com/reuters/financialNews'),
            ('AP Business', 'https://feeds.apnews.com/apnews/business'),
            ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex'),
            ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories'),
            ('MarketWatch Real-time', 'https://feeds.marketwatch.com/marketwatch/realtimeheadlines'),
        ]

        for source, feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                count = 0
                for entry in feed.entries[:25]:
                    url = entry.get('link', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        content = entry.get('summary', '') or ''
                        if content:
                            content = BeautifulSoup(content, 'html.parser').get_text()[:500]
                        all_news.append({
                            'title': entry.get('title', ''),
                            'description': entry.get('summary', '')[:200],
                            'url': url,
                            'source': source,
                            'published': entry.get('published', ''),
                            'content': content,
                            'collected_at': self.now.isoformat()
                        })
                        count += 1
                print(f"   RSS {source}: {count}개")
            except Exception as e:
                print(f"   ❌ RSS {source}: {e}")

        print(f"✅ 총 {len(all_news)}개 수집")
        return all_news

    # --------------------------------------------------------
    # 관련 뉴스 필터링
    # --------------------------------------------------------
    def filter_relevant_news(self, all_news, seen_ids):
        """새 뉴스 + 관련 뉴스만 필터링"""
        new_relevant = []

        for news in all_news:
            url = news.get('url', '')
            if not url or url in seen_ids:
                continue

            title = news.get('title', '').lower()
            desc = news.get('description', '').lower()
            text = title + ' ' + desc

            # 키워드 매칭
            is_relevant = any(kw in text for kw in self.keywords)

            if is_relevant:
                new_relevant.append(news)
                seen_ids.add(url)

        print(f"✨ 새 관련 뉴스: {len(new_relevant)}개")
        return new_relevant, seen_ids

    # --------------------------------------------------------
    # URL 크롤링 (본문 없는 것만)
    # --------------------------------------------------------
    def crawl_content(self, news_list, max_count=15):
        crawled = 0
        for news in news_list:
            if not news['content'] and news['url'] and crawled < max_count:
                try:
                    resp = requests.get(
                        news['url'], timeout=5,
                        headers={'User-Agent': 'Mozilla/5.0'}
                    )
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                            tag.decompose()
                        content = ' '.join([p.get_text() for p in soup.find_all('p')[:8]])[:600]
                        news['content'] = content
                        crawled += 1
                except:
                    pass
        print(f"🕷️ 크롤링 완료: {crawled}개")
        return news_list

    # --------------------------------------------------------
    # 메인 실행
    # --------------------------------------------------------
    def run(self):
        print("\n" + "=" * 50)
        print(f"🌙 장후 뉴스 수집: {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
        print("=" * 50)

        # 기존 누적 뉴스 로드
        existing_news = self.load_afterhours_news()
        seen_ids = self.load_seen_ids()
        print(f"📋 기존 누적: {len(existing_news)}개 | 이미 본 URL: {len(seen_ids)}개")

        # 새 뉴스 수집
        all_news = self.collect_news()

        # 필터링
        new_relevant, seen_ids = self.filter_relevant_news(all_news, seen_ids)

        if not new_relevant:
            print("✅ 새 관련 뉴스 없음 - 종료")
            return

        # 크롤링
        new_relevant = self.crawl_content(new_relevant)

        # 누적 저장 (최대 200개)
        combined = existing_news + new_relevant
        combined = combined[-200:]

        self.save_afterhours_news(combined)
        self.save_seen_ids(seen_ids)

        print(f"\n✅ 완료! 총 누적: {len(combined)}개 (+{len(new_relevant)}개)")


if __name__ == "__main__":
    collector = AfterhoursCollector()
    collector.run()
