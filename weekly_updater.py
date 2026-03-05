import os
import yaml
import json
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from groq import Groq
try:
    from google import genai as genai_new
    USE_NEW_GENAI = True
except ImportError:
    import google.generativeai as genai
    USE_NEW_GENAI = False
import pytz


class WeeklyUpdater:
    """매주 핫종목 자동 발견 & 업데이트 v4.0"""

    def __init__(self):
        print("🔄 Weekly Updater v4.0 시작...")

        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.gemini_api_key = os.environ.get('GEMINI_API_KEY', '')

        self.groq = Groq(api_key=self.groq_api_key)
        self.gemini = None
        if self.gemini_api_key:
            try:
                if USE_NEW_GENAI:
                    self.gemini = genai_new.Client(api_key=self.gemini_api_key)
                else:
                    genai.configure(api_key=self.gemini_api_key)
                    self.gemini = genai.GenerativeModel('gemini-1.5-flash')
            except Exception as e:
                print(f"⚠️ Gemini 초기화 실패: {e}")

        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)

        # 암호화폐 제외 목록
        self.crypto_tickers = [
            'BTC', 'ETH', 'COIN', 'MARA', 'RIOT', 'MSTR',
            'BITF', 'HUT', 'CLSK', 'BTBT', 'SOS', 'CAN',
            'GBTC', 'ETHE', 'BITO', 'BKCH', 'IREN'
        ]

        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d')}")

    # --------------------------------------------------------
    # 뉴스 수집 (NewsAPI top-headlines + RSS)
    # --------------------------------------------------------
    def collect_weekly_news(self):
        print("\n📰 주간 뉴스 수집 중...")
        all_news = []
        seen_urls = set()

        # 1. NewsAPI top-headlines (무료)
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
                            all_news.append(
                                f"{a.get('title', '')} {a.get('description', '')}"
                            )
                print(f"   NewsAPI {category}: {len(all_news)}개")
            except Exception as e:
                print(f"   ❌ NewsAPI {category}: {e}")

        # 2. RSS 피드 (주간 분석용)
        rss_feeds = [
            ('Reuters', 'https://feeds.reuters.com/reuters/businessNews'),
            ('Reuters Finance', 'https://feeds.reuters.com/reuters/financialNews'),
            ('AP Business', 'https://feeds.apnews.com/apnews/business'),
            ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex'),
            ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories'),
            ('Seeking Alpha', 'https://seekingalpha.com/feed.xml'),
        ]

        for source, feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:30]:
                    url = entry.get('link', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        content = entry.get('summary', '') or ''
                        if content:
                            content = BeautifulSoup(content, 'html.parser').get_text()[:300]
                        all_news.append(f"{entry.get('title', '')} {content}")
                print(f"   RSS {source}: {len(feed.entries[:30])}개")
            except Exception as e:
                print(f"   ❌ RSS {source}: {e}")

        print(f"✅ 총 {len(all_news)}개 뉴스 수집")
        return all_news

    # --------------------------------------------------------
    # 핫종목 추출 (Groq → Gemini 폴백)
    # --------------------------------------------------------
    def extract_hot_tickers(self, news_list):
        print("\n🔥 핫종목 추출 중...")

        all_text = "\n".join(news_list[:80])[:4000]

        prompt = f"""Analyze this week's financial news and extract the hottest stock tickers.

NEWS:
{all_text}

Rules:
1. Extract ONLY ETF or stock tickers (e.g., AAPL, TSLA, NVDA, QQQ)
2. EXCLUDE ALL crypto: {', '.join(self.crypto_tickers)}
3. Only if mentioned 2+ times across different articles
4. US-listed only (no .TO .L suffixes)
5. Focus on actual momentum/news-driven stocks

Return ONLY JSON:
{{
  "tickers": ["TICKER1", "TICKER2", ...],
  "reasons": {{"TICKER1": "why hot in one sentence", ...}}
}}

Top 5 only. JSON only, no markdown."""

        # Groq 시도
        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.2,
                max_tokens=600,
            )
            text = response.choices[0].message.content
            text = text.replace('```json', '').replace('```', '').strip()
            result = json.loads(text)
        except Exception as e:
            print(f"   ⚠️ Groq 오류: {e}, Gemini 폴백...")
            try:
                response = self.gemini.generate_content(prompt)
                text = response.text.replace('```json', '').replace('```', '').strip()
                result = json.loads(text)
            except Exception as e2:
                print(f"   ❌ Gemini 오류: {e2}")
                return [], {}

        tickers = result.get('tickers', [])
        reasons = result.get('reasons', {})

        # 암호화폐 재필터
        filtered = [t for t in tickers if t not in self.crypto_tickers]

        print(f"✅ {len(filtered)}개 핫종목 발견")
        for ticker in filtered:
            print(f"   🔥 {ticker}: {reasons.get(ticker, '')}")

        return filtered, reasons

    # --------------------------------------------------------
    # portfolio.yaml 업데이트
    # --------------------------------------------------------
    def update_portfolio_yaml(self, new_tickers, reasons):
        print("\n📝 portfolio.yaml 업데이트 중...")

        try:
            with open('portfolio.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            current_alts = config.get('alternative_assets', [])
            current_tickers = [a['ticker'] for a in current_alts]
            cutoff_date = (self.now - timedelta(days=21)).strftime('%Y-%m-%d')

            # 새 종목 추가
            added = []
            for ticker in new_tickers:
                if ticker not in current_tickers:
                    current_alts.append({
                        'ticker': ticker,
                        'name': reasons.get(ticker, 'Hot stock'),
                        'mer': 0,
                        'keywords': [ticker.lower()],
                        'added': self.now.strftime('%Y-%m-%d'),
                        'auto_added': True
                    })
                    added.append(ticker)
                    print(f"   ➕ {ticker} 추가")

            # 3주 이상 된 자동추가 종목 제거
            removed = []
            filtered_alts = []
            for asset in current_alts:
                if asset.get('auto_added'):
                    added_date = asset.get('added', '2020-01-01')
                    if added_date < cutoff_date and asset['ticker'] not in new_tickers:
                        removed.append(asset['ticker'])
                        print(f"   🗑️ {asset['ticker']} 제거 (3주 경과)")
                        continue
                filtered_alts.append(asset)

            config['alternative_assets'] = filtered_alts

            with open('portfolio.yaml', 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False,
                         allow_unicode=True, sort_keys=False)

            print(f"✅ 업데이트 완료 (추가 {len(added)}개 / 제거 {len(removed)}개)")
            return added, removed

        except Exception as e:
            print(f"❌ YAML 업데이트 오류: {e}")
            return [], []

    # --------------------------------------------------------
    # 메인 실행
    # --------------------------------------------------------
    def run(self):
        print("\n" + "=" * 50)
        print("🤖 Weekly Auto-Update v4.0")
        print("=" * 50)

        news = self.collect_weekly_news()

        if not news:
            print("\n⚠️ 뉴스 없음, 오래된 종목만 정리")
            self.update_portfolio_yaml([], {})
            return

        hot_tickers, reasons = self.extract_hot_tickers(news)
        added, removed = self.update_portfolio_yaml(hot_tickers, reasons)

        print("\n" + "=" * 50)
        print("📊 주간 업데이트 완료")
        if added:
            print(f"✅ 추가: {', '.join(added)}")
        if removed:
            print(f"🗑️ 제거: {', '.join(removed)}")
        if not added and not removed:
            print("✅ 변경사항 없음")
        print("=" * 50)


if __name__ == "__main__":
    updater = WeeklyUpdater()
    updater.run()
