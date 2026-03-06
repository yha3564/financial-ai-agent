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
import asyncio
import pytz
import yfinance as yf
import pandas as pd
import numpy as np
import pandas_ta_classic as ta
import time
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

# ============================================================
# 가격 캐시 클래스 (Yahoo 429 방지)
# ============================================================
class PriceCache:
    def __init__(self):
        self._price_cache = {}
        self._hist_cache = {}

    def batch_download(self, tickers):
        """전체 자산 한번에 다운로드 후 파싱"""
        print(f"\n📥 자산 가격 다운로드 중 ({len(tickers)}개)...")

        try:
            tickers_str = " ".join(tickers)
            df = yf.download(tickers_str, period="5d", auto_adjust=True,
                           progress=False, threads=False)

            if df.empty:
                print("⚠️ 전체 다운로드 실패 - 개별 다운로드 시도...")
                self._fallback_download(tickers)
                return

            close = df['Close'] if 'Close' in df.columns else df.xs('Close', axis=1, level=0)

            if isinstance(close, pd.Series):
                ticker = tickers[0]
                data = close.dropna()
                if len(data) > 0:
                    self._price_cache[ticker] = float(data.iloc[-1])
                    self._hist_cache[ticker] = pd.DataFrame({'Close': data})
            else:
                for ticker in tickers:
                    try:
                        if ticker in close.columns:
                            data = close[ticker].dropna()
                            if len(data) > 0:
                                self._price_cache[ticker] = float(data.iloc[-1])
                                self._hist_cache[ticker] = pd.DataFrame({'Close': data})
                            else:
                                self._price_cache[ticker] = 0
                        else:
                            self._price_cache[ticker] = 0
                    except:
                        self._price_cache[ticker] = 0

            loaded = sum(1 for v in self._price_cache.values() if v > 0)
            print(f"✅ 가격 로드 완료 ({loaded}/{len(tickers)}개 성공)")
            if loaded == 0:
                raise RuntimeError(f"가격 다운로드 전부 실패 ({len(tickers)}개) - yfinance 또는 Yahoo 연결 문제")
            
            failed = [t for t in tickers if self._price_cache.get(t, 0) == 0]
            if failed:
                print(f"⚠️ {len(failed)}개 개별 재시도...")
                self._fallback_download(failed)

        except Exception as e:
            print(f"⚠️ 전체 다운로드 오류: {e} - 개별 다운로드 시도...")
            self._fallback_download(tickers)

    def _fallback_download(self, tickers):
        """개별 티커 다운로드 (폴백)"""
        for ticker in tickers:
            try:
                df = yf.download(ticker, period="5d", auto_adjust=True,
                               progress=False, threads=False)
                if not df.empty and 'Close' in df.columns:
                    data = df['Close'].dropna()
                    if len(data) > 0:
                        self._price_cache[ticker] = float(data.iloc[-1])
                        self._hist_cache[ticker] = pd.DataFrame({'Close': data})
                        continue
            except:
                pass
            self._price_cache[ticker] = 0
            time.sleep(0.5)

    def get_price(self, ticker):
        return self._price_cache.get(ticker, 0)

    def get_hist(self, ticker):
        return self._hist_cache.get(ticker, pd.DataFrame())


# ============================================================
# 메인 클래스
# ============================================================
class DailyDigest:
    """아침 종합 브리핑 v4.0"""

    def __init__(self):
        print("🚀 Daily Digest v4.0 초기화...")

        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.gemini_api_key = os.environ.get('GEMINI_API_KEY', '')
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']

        self.groq = Groq(api_key=self.groq_api_key)
        self.gemini = None
        if self.gemini_api_key:
            try:
                if USE_NEW_GENAI:
                    self.gemini_client = genai_new.Client(api_key=self.gemini_api_key)
                    self.gemini = self.gemini_client
                else:
                    genai.configure(api_key=self.gemini_api_key)
                    self.gemini = genai.GenerativeModel('gemini-1.5-flash')
                print("✅ Gemini 초기화 완료")
            except Exception as e:
                print(f"⚠️ Gemini 초기화 실패 (Groq만 사용): {e}")

        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)

        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.cost_settings = self.config.get('cost_settings', {})
        self.trading_rules = self.config.get('trading_rules', {})
        self.alternative_assets = [a['ticker'] for a in self.config.get('alternative_assets', [])]
        self.safe_assets = [a['ticker'] for a in self.config.get('safe_assets', [])]

        self.mer_map = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.mer_map[asset['ticker']] = asset.get('mer', 0.003)

        self.load_portfolio()

        self.price_cache = PriceCache()
        all_tickers = list(set(
            list(self.my_holdings_tfsa1.keys()) +
            list(self.my_holdings_tfsa2.keys()) +
            self.alternative_assets +
            self.safe_assets
        ))
        self.price_cache.batch_download(all_tickers)
        self.all_tracked_assets = all_tickers

        self.build_ticker_name_map()
        self.check_monthly_cash()

        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"📊 추적 자산: {len(self.all_tracked_assets)}개")
        print(f"💵 누적 현금: ${self.accumulated_cash:.0f}")

    # --------------------------------------------------------
    # 포트폴리오 로드/저장
    # --------------------------------------------------------
    def load_portfolio(self):
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
            self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
            self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
            self.accumulated_cash = portfolio.get('accumulated_cash', 0)
            self.last_cash_added = portfolio.get('last_cash_added', '')
            print(f"📂 current_portfolio.json 로드")
        except FileNotFoundError:
            print(f"📂 portfolio.yaml에서 초기화...")
            self.my_holdings_tfsa1 = {}
            for asset in self.config.get('tfsa1_assets', []):
                self.my_holdings_tfsa1[asset['ticker']] = {
                    'shares': asset.get('shares', 0),
                    'avg_price': asset.get('avg_price', 0)
                }
            self.my_holdings_tfsa2 = {}
            for asset in self.config.get('tfsa2_assets', []):
                self.my_holdings_tfsa2[asset['ticker']] = {
                    'shares': asset.get('shares', 0),
                    'avg_price': asset.get('avg_price', 0),
                    'purpose': asset.get('purpose', ''),
                    'target_amount': asset.get('target_amount', 0)
                }
            monthly = self.config.get('monthly_cash_inflow', {})
            self.accumulated_cash = monthly.get('tfsa1', 0)
            self.last_cash_added = ''
            self.save_portfolio()

        for asset in self.config.get('tfsa2_assets', []):
            ticker = asset['ticker']
            if ticker in self.my_holdings_tfsa2:
                self.my_holdings_tfsa2[ticker]['purpose'] = asset.get('purpose', '')
                self.my_holdings_tfsa2[ticker]['target_amount'] = asset.get('target_amount', 0)

        print(f"💼 TFSA 1: {list(self.my_holdings_tfsa1.keys())}")
        print(f"💰 TFSA 2: {list(self.my_holdings_tfsa2.keys())}")

    def save_portfolio(self):
        portfolio = {
            'tfsa1': self.my_holdings_tfsa1,
            'tfsa2': self.my_holdings_tfsa2,
            'accumulated_cash': self.accumulated_cash,
            'last_cash_added': self.last_cash_added,
            'date': self.now.strftime('%Y-%m-%d'),
            'time': self.now.strftime('%H:%M')
        }
        with open('current_portfolio.json', 'w', encoding='utf-8') as f:
            json.dump(portfolio, f, indent=2, ensure_ascii=False)
        print(f"💾 포트폴리오 저장 완료")

    def check_monthly_cash(self):
        today = self.now.strftime('%Y-%m-%d')
        if self.now.day == 1 and self.last_cash_added != today:
            monthly = self.config.get('monthly_cash_inflow', {})
            amount = monthly.get('tfsa1', 0)
            self.accumulated_cash += amount
            self.last_cash_added = today
            print(f"💵 월간 현금 입금: +${amount} (총: ${self.accumulated_cash:.0f})")
            self.save_portfolio()

    def build_ticker_name_map(self):
        self.ticker_names = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.ticker_names[asset['ticker']] = asset.get('name', asset['ticker'])

    # --------------------------------------------------------
    # 가격 조회 (캐시)
    # --------------------------------------------------------
    def get_price(self, ticker):
        return self.price_cache.get_price(ticker)

    def get_current_value(self, ticker, holding):
        shares = holding.get('shares', 0)
        price = self.get_price(ticker)
        return shares * price if price > 0 else 0

    def get_profit_pct(self, ticker, holding):
        avg_price = holding.get('avg_price', 0)
        current_price = self.get_price(ticker)
        if avg_price > 0 and current_price > 0:
            return (current_price - avg_price) / avg_price * 100
        return 0

    # --------------------------------------------------------
    # 뉴스 수집
    # --------------------------------------------------------
    def collect_all_news(self):
        print("\n📰 뉴스 수집 중...")
        all_news = []
        seen_urls = set()

        for category in ['business', 'technology']:
            try:
                response = requests.get(
                    "https://newsapi.org/v2/top-headlines",
                    params={'language': 'en', 'apiKey': self.news_api_key,
                            'pageSize': 100, 'category': category, 'country': 'us'},
                    timeout=10
                )
                if response.status_code == 200:
                    articles = response.json().get('articles', [])
                    for a in articles:
                        url = a.get('url', '')
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            all_news.append({
                                'title': a.get('title', ''),
                                'description': a.get('description', ''),
                                'url': url,
                                'source': a.get('source', {}).get('name', ''),
                                'content': ''
                            })
                    print(f"   NewsAPI {category}: {len(articles)}개")
            except Exception as e:
                print(f"   ❌ NewsAPI {category} 오류: {e}")

        rss_feeds = [
            ('Reuters', 'https://feeds.reuters.com/reuters/businessNews'),
            ('AP News', 'https://feeds.apnews.com/apnews/business'),
            ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex'),
            ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories'),
        ]
        for source, feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:
                    url = entry.get('link', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        content = entry.get('summary', '') or entry.get('content', [{}])[0].get('value', '')
                        if content:
                            content = BeautifulSoup(content, 'html.parser').get_text()[:500]
                        all_news.append({
                            'title': entry.get('title', ''),
                            'description': entry.get('summary', '')[:200],
                            'url': url,
                            'source': source,
                            'content': content
                        })
                print(f"   RSS {source}: {len(feed.entries[:20])}개")
            except Exception as e:
                print(f"   ❌ RSS {source} 오류: {e}")

        crawl_count = 0
        for news in all_news:
            if not news['content'] and news['url'] and crawl_count < 30:
                try:
                    resp = requests.get(news['url'], timeout=5,
                                       headers={'User-Agent': 'Mozilla/5.0'})
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                            tag.decompose()
                        paragraphs = soup.find_all('p')
                        content = ' '.join([p.get_text() for p in paragraphs[:10]])[:800]
                        news['content'] = content
                        crawl_count += 1
                except:
                    pass

        print(f"✅ 총 {len(all_news)}개 수집 (크롤링 {crawl_count}개)")
        return all_news[:100]

    # --------------------------------------------------------
    # 장후 뉴스 로드
    # --------------------------------------------------------
    def load_afterhours_news(self):
        try:
            with open('afterhours_news.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            news_list = data.get('news', [])
            print(f"📌 장후 뉴스 로드: {len(news_list)}개")
            return news_list
        except FileNotFoundError:
            print("📌 장후 뉴스 없음")
            return []

    def clear_afterhours_news(self):
        with open('afterhours_news.json', 'w', encoding='utf-8') as f:
            json.dump({'news': [], 'date': self.now.strftime('%Y-%m-%d')}, f)

    # --------------------------------------------------------
    # AI 뉴스 분석
    # --------------------------------------------------------
    def analyze_news_batch(self, news_batch):
        assets_str = ", ".join(self.all_tracked_assets)

        news_text = ""
        for i, n in enumerate(news_batch, 1):
            body = n.get('content') or n.get('description') or ''
            news_text += f"[{i}] {n['title']}\n{body[:300]}\n\n"

        prompt = f"""Analyze these {len(news_batch)} news articles and their impact on assets.

NEWS:
{news_text}

ASSETS TO ANALYZE:
{assets_str}

For each asset significantly impacted, return JSON:
{{
  "TICKER": {{"impact": "bullish/bearish/neutral", "magnitude": 0.XX, "confidence": XX, "reason": "brief reason"}},
  ...
}}

Rules:
- magnitude: 0.01 to 0.50 (estimated price change %)
- confidence: 0 to 100
- Only include assets with REAL significant impact
- neutral assets: skip entirely
- Return ONLY valid JSON, no markdown"""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.3,
                max_tokens=2000,
            )
            text = response.choices[0].message.content
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
        except Exception as e:
            print(f"   ⚠️ Groq 오류: {e}, Gemini 폴백...")

        if not self.gemini:
            return {}
        try:
            if USE_NEW_GENAI:
                response = self.gemini.models.generate_content(
                    model='gemini-1.5-flash', contents=prompt)
                text = response.text
            else:
                response = self.gemini.generate_content(prompt)
                text = response.text
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
        except Exception as e:
            print(f"   ❌ Gemini 오류: {e}")
            return {}

    def aggregate_asset_impacts(self, all_news):
        print("\n🧠 AI 뉴스 영향 분석 중 (배치 처리)...")

        asset_scores = {ticker: {'total': 0, 'confidences': [], 'reasons': [], 'count': 0}
                        for ticker in self.all_tracked_assets}

        batch_size = 10
        batches = [all_news[i:i+batch_size] for i in range(0, len(all_news), batch_size)]

        for i, batch in enumerate(batches):
            print(f"   배치 [{i+1}/{len(batches)}] {len(batch)}개 뉴스 분석 중...")
            impacts = self.analyze_news_batch(batch)

            for ticker, data in impacts.items():
                if ticker in asset_scores:
                    try:
                        magnitude = float(data.get('magnitude', 0))
                        confidence = float(data.get('confidence', 50)) / 100
                        impact = data.get('impact', 'neutral')
                        reason = data.get('reason', '')

                        if impact == 'bearish':
                            magnitude = -magnitude
                        elif impact == 'neutral':
                            magnitude = 0

                        asset_scores[ticker]['total'] += magnitude
                        asset_scores[ticker]['confidences'].append(confidence)
                        asset_scores[ticker]['reasons'].append(reason)
                        asset_scores[ticker]['count'] += 1
                    except:
                        pass

        results = {}
        for ticker, data in asset_scores.items():
            if data['count'] > 0:
                avg_conf = sum(data['confidences']) / len(data['confidences'])
                weighted_score = data['total'] * avg_conf
                results[ticker] = {
                    'magnitude': data['total'],
                    'confidence': avg_conf,
                    'weighted_score': weighted_score,
                    'reasons': data['reasons'][:3],
                    'news_count': data['count']
                }
            else:
                results[ticker] = {
                    'magnitude': 0,
                    'confidence': 0.5,
                    'weighted_score': 0,
                    'reasons': [],
                    'news_count': 0
                }

        print(f"✅ 분석 완료 (배치 {len(batches)}번 호출)")
        return results

    # --------------------------------------------------------
    # 장후 뉴스 요약 분석
    # --------------------------------------------------------
    def analyze_afterhours_summary(self, afterhours_news):
        if not afterhours_news:
            return None

        news_text = ""
        for n in afterhours_news[:30]:
            news_text += f"- {n.get('title', '')}\n"

        prompt = f"""다음 장후 뉴스들을 분석해서 한국어로 요약해줘:

{news_text}

다음 형식으로:
{{
  "conclusion": "호재우세/악재우세/중립",
  "bullish_count": 숫자,
  "bearish_count": 숫자,
  "neutral_count": 숫자,
  "key_bullish": [{{"headline": "주요 호재 1", "assets": ["TICKER1", "TICKER2"]}}, ...],
  "key_bearish": [{{"headline": "주요 악재 1", "assets": ["TICKER1"]}}, ...],
  "summary": "전체 흐름 한 줄 요약"
}}

JSON만 반환. key_bullish/key_bearish의 assets는 영향받는 보유자산 티커 목록."""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.3,
                max_tokens=600,
            )
            text = response.choices[0].message.content
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
        except:
            if not self.gemini:
                return None
            try:
                if USE_NEW_GENAI:
                    response = self.gemini.models.generate_content(
                        model='gemini-1.5-flash', contents=prompt)
                    text = response.text
                else:
                    response = self.gemini.generate_content(prompt)
                    text = response.text
                text = text.replace('```json', '').replace('```', '').strip()
                return json.loads(text)
            except:
                return None

    # --------------------------------------------------------
    # 기술적 분석
    # --------------------------------------------------------
    def technical_analysis(self, ticker):
        try:
            df = self.price_cache.get_hist(ticker)
            if df.empty or len(df) < 20:
                return {'signal': 'neutral', 'rsi': 50}

            df['rsi'] = ta.rsi(df['Close'], length=14)
            current_rsi = float(df['rsi'].iloc[-1])

            macd_df = ta.macd(df['Close'])
            df = df.join(macd_df)
            macd_bullish = df['MACD_12_26_9'].iloc[-1] > df['MACDs_12_26_9'].iloc[-1]

            df['ma_20'] = df['Close'].rolling(20).mean()
            df['ma_50'] = df['Close'].rolling(50).mean()
            above_ma = df['Close'].iloc[-1] > df['ma_20'].iloc[-1]

            if current_rsi < 30 and macd_bullish:
                return {'signal': 'buy', 'rsi': current_rsi}
            elif current_rsi > 70:
                return {'signal': 'sell', 'rsi': current_rsi}
            else:
                return {'signal': 'neutral', 'rsi': current_rsi}
        except:
            return {'signal': 'neutral', 'rsi': 50}

    # --------------------------------------------------------
    # 순위표 생성
    # --------------------------------------------------------
    def create_rankings(self, asset_impacts):
        print("\n📊 순위표 생성 중...")
        fx_fee = self.cost_settings.get('fx_fee', 0.015)
        rankings = []

        for ticker in self.all_tracked_assets:
            impact = asset_impacts.get(ticker, {})
            weighted_score = impact.get('weighted_score', 0)
            magnitude = impact.get('magnitude', 0)
            confidence = impact.get('confidence', 0.5)
            reasons = impact.get('reasons', [])

            tech = self.technical_analysis(ticker)
            if tech['signal'] == 'sell' and weighted_score > 0:
                weighted_score *= 0.8
            elif tech['signal'] == 'buy' and weighted_score < 0:
                weighted_score *= 0.8

            mer = self.mer_map.get(ticker, 0.003)
            is_usd = not ticker.endswith('.TO')
            net_cost = (fx_fee * 2 if is_usd else 0) + (mer * 3 / 12)

            rankings.append({
                'ticker': ticker,
                'weighted_score': weighted_score,
                'magnitude': magnitude,
                'confidence': confidence,
                'net_cost': net_cost,
                'net_score': weighted_score - net_cost,
                'reasons': reasons,
                'technical': tech,
                'news_count': impact.get('news_count', 0)
            })

        rankings.sort(key=lambda x: x['weighted_score'], reverse=True)
        print(f"✅ 순위표 완료 ({len(rankings)}개)")
        return rankings

    # --------------------------------------------------------
    # 추천 생성
    # IMP-006/007: 모든 매도 후 즉시 대안자산 재투자 (현금 대기 금지)
    #              예외: 스캔한 모든 alternative_assets 점수 음수 시만 현금 보유
    # IMP-002:     각 액션에 recommended_price / recommended_at 저장
    # --------------------------------------------------------
    def generate_recommendations(self, rankings):
        print("\n💡 추천 생성 중...")

        rankings_map = {r['ticker']: r for r in rankings}
        tfsa1_rules = self.trading_rules.get('tfsa1', {})
        tfsa2_rules = self.trading_rules.get('tfsa2', {})
        concentration_threshold = tfsa1_rules.get('concentration_threshold', 0.10)
        alert_threshold = self.config.get('ranking_rules', {}).get('alert_threshold', 0.15)
        recommended_at = self.now.isoformat()

        # ── TFSA 1 ──
        tfsa1_actions = []
        available_cash = self.accumulated_cash
        sell_proceeds = 0

        partial_threshold = tfsa1_rules.get('partial_sell_threshold', 0.15)
        half_threshold = tfsa1_rules.get('half_sell_threshold', 0.25)
        full_threshold = tfsa1_rules.get('full_sell_threshold', 0.35)

        # STEP 1: 매도 판단
        for ticker, holding in self.my_holdings_tfsa1.items():
            rank = rankings_map.get(ticker, {})
            score = rank.get('weighted_score', 0)
            current_price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            current_value = shares * current_price

            if score <= -full_threshold:
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': current_price, 'value': current_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': current_price,   # IMP-002
                    'recommended_at': recommended_at       # IMP-002
                })
                sell_proceeds += current_value

            elif score <= -half_threshold:
                sell_shares = round(shares * 0.5, 4)
                sell_value = sell_shares * current_price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'half',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': current_price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': current_price,
                    'recommended_at': recommended_at
                })
                sell_proceeds += sell_value

            elif score <= -partial_threshold:
                sell_pct = 0.3 + (abs(score) - partial_threshold) / (half_threshold - partial_threshold) * 0.2
                sell_shares = round(shares * sell_pct, 4)
                sell_value = sell_shares * current_price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'partial',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': current_price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': current_price,
                    'recommended_at': recommended_at
                })
                sell_proceeds += sell_value

        available_cash += sell_proceeds
        sold_tickers = [a['ticker'] for a in tfsa1_actions if a['action'] == 'SELL' and a['type'] == 'full']

        # STEP 2: 매수 후보
        buy_candidates = [
            r for r in rankings
            if r['weighted_score'] > alert_threshold
            and r['ticker'] not in sold_tickers
            and self.get_price(r['ticker']) > 0
        ]
        buy_candidates = sorted(buy_candidates, key=lambda x: x['net_score'], reverse=True)[:5]

        # IMP-006/007: 매도금이 있을 때 반드시 재투자
        # 매수 후보가 없거나 모두 음수이면 현금 보유 (예외)
        has_positive_candidate = any(r['weighted_score'] > 0 for r in rankings
                                     if r['ticker'] not in sold_tickers
                                     and self.get_price(r['ticker']) > 0)

        if available_cash > 0 and buy_candidates and has_positive_candidate:
            top1 = buy_candidates[0]
            if len(buy_candidates) >= 2:
                diff = top1['weighted_score'] - buy_candidates[1]['weighted_score']
                buy_list = [top1] if diff >= concentration_threshold else [top1, buy_candidates[1]]
            else:
                buy_list = [top1]

            per_amount = available_cash / len(buy_list)
            for candidate in buy_list:
                price = self.get_price(candidate['ticker'])
                if price > 0:
                    shares = round(per_amount / price, 4)
                    tfsa1_actions.append({
                        'action': 'BUY',
                        'ticker': candidate['ticker'],
                        'shares': shares,
                        'price': price,
                        'value': per_amount,
                        'score': candidate['weighted_score'],
                        'expected_pct': candidate['magnitude'] * 100,
                        'recommended_price': price,     # IMP-002
                        'recommended_at': recommended_at # IMP-002
                    })

        # STEP 4: 스왑 판단
        has_buy = any(a['action'] == 'BUY' for a in tfsa1_actions)
        if not has_buy and buy_candidates and has_positive_candidate:
            top1 = buy_candidates[0]
            held = []
            for t in self.my_holdings_tfsa1:
                if t in sold_tickers or t == top1['ticker']:
                    continue
                if self.get_price(t) <= 0:
                    continue
                if t in rankings_map:
                    held.append(rankings_map[t])
                else:
                    held.append({
                        'ticker': t, 'weighted_score': 0, 'magnitude': 0,
                        'net_score': 0, 'net_cost': 0, 'reasons': [], 'news_count': 0
                    })
            if held:
                weakest = min(held, key=lambda x: x['weighted_score'])
                if top1['weighted_score'] - weakest['weighted_score'] >= alert_threshold:
                    w_ticker = weakest['ticker']
                    w_holding = self.my_holdings_tfsa1.get(w_ticker, {})
                    w_shares = w_holding.get('shares', 0)
                    w_price = self.get_price(w_ticker)
                    w_value = w_shares * w_price
                    print(f"   🔄 스왑: {w_ticker}(score={weakest['weighted_score']:.3f}) → {top1['ticker']}(score={top1['weighted_score']:.3f})")
                    tfsa1_actions.append({
                        'action': 'SELL', 'type': 'full',
                        'ticker': w_ticker, 'shares': w_shares,
                        'price': w_price, 'value': w_value,
                        'score': weakest['weighted_score'],
                        'expected_pct': weakest['magnitude'] * 100,
                        'recommended_price': w_price,
                        'recommended_at': recommended_at
                    })
                    b_price = self.get_price(top1['ticker'])
                    if b_price > 0:
                        total_buy = w_value + available_cash
                        b_shares = round(total_buy / b_price, 4)
                        tfsa1_actions.append({
                            'action': 'BUY',
                            'ticker': top1['ticker'],
                            'shares': b_shares,
                            'price': b_price,
                            'value': total_buy,
                            'score': top1['weighted_score'],
                            'expected_pct': top1['magnitude'] * 100,
                            'recommended_price': b_price,
                            'recommended_at': recommended_at
                        })

        # ── TFSA 2 ──
        tfsa2_actions = {}
        full_threshold_t2 = tfsa2_rules.get('full_sell_threshold', 0.30)
        safe_rankings = [r for r in rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['net_score'], reverse=True)

        for ticker, holding in self.my_holdings_tfsa2.items():
            purpose = holding.get('purpose', ticker)
            rank = rankings_map.get(ticker, {})
            score = rank.get('weighted_score', 0)
            current_price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            current_value = shares * current_price

            actions = []

            best_alternative = None
            for safe in safe_rankings:
                if safe['ticker'] != ticker and safe['net_score'] > score + 0.05:
                    best_alternative = safe
                    break

            if score <= -full_threshold_t2 and best_alternative:
                best_price = self.get_price(best_alternative['ticker'])
                buy_shares = round(current_value / best_price, 4) if best_price > 0 else 0
                actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': current_price, 'value': current_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': current_price,
                    'recommended_at': recommended_at
                })
                actions.append({
                    'action': 'BUY',
                    'ticker': best_alternative['ticker'],
                    'shares': buy_shares,
                    'price': best_price,
                    'value': current_value,
                    'score': best_alternative['weighted_score'],
                    'expected_pct': best_alternative['magnitude'] * 100,
                    'recommended_price': best_price,
                    'recommended_at': recommended_at
                })
            elif best_alternative and best_alternative['net_score'] - score > 0.15:
                best_price = self.get_price(best_alternative['ticker'])
                buy_shares = round(current_value / best_price, 4) if best_price > 0 else 0
                actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': current_price, 'value': current_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': current_price,
                    'recommended_at': recommended_at
                })
                actions.append({
                    'action': 'BUY',
                    'ticker': best_alternative['ticker'],
                    'shares': buy_shares,
                    'price': best_price,
                    'value': current_value,
                    'score': best_alternative['weighted_score'],
                    'expected_pct': best_alternative['magnitude'] * 100,
                    'recommended_price': best_price,
                    'recommended_at': recommended_at
                })
            else:
                actions.append({'action': 'HOLD', 'ticker': ticker})

            tfsa2_actions[ticker] = {
                'purpose': purpose,
                'current_value': current_value,
                'actions': actions
            }

        return {
            'tfsa1': tfsa1_actions,
            'tfsa2': tfsa2_actions,
            'rankings': rankings[:10],
            'available_cash': available_cash
        }

    # --------------------------------------------------------
    # 텔레그램 리포트 생성
    # IMP-005: 장후 뉴스 헤드라인 아래 "   → 영향자산" 들여쓰기 구조
    # IMP-011: 매수 섹션 "💰 매수가능" + 자산명(풀네임) + 주수@가격 포맷
    # IMP-013: TFSA 2 항상 표시 (HOLD면 "전체 유지" 출력)
    # --------------------------------------------------------
    def format_telegram_report(self, recommendations, afterhours_summary=None):
        report = f"📊 데일리 브리핑\n🕐 {self.now.strftime('%Y-%m-%d %H:%M EST')}\n"
        report += "=" * 37 + "\n"

        # IMP-005: 뉴스 섹션 — 헤드라인 아래 영향자산 들여쓰기
        if afterhours_summary:
            conclusion = afterhours_summary.get('conclusion', '')
            conclusion_emoji = "🟢" if "호재" in conclusion else "🔴" if "악재" in conclusion else "⚪"
            bullish_count = afterhours_summary.get('bullish_count', 0)
            bearish_count = afterhours_summary.get('bearish_count', 0)
            report += f"{conclusion_emoji} {conclusion} | 호재 {bullish_count}건 | 악재 {bearish_count}건\n"

            # key_bullish: 헤드라인 + 영향자산 들여쓰기
            for item in afterhours_summary.get('key_bullish', []):
                if isinstance(item, dict):
                    headline = item.get('headline', '')
                    assets = item.get('assets', [])
                else:
                    headline = item
                    assets = []
                report += f"🟢 {headline}\n"
                if assets:
                    asset_names = " · ".join(
                        f"{t}({self.ticker_names.get(t, t)})" for t in assets
                        if t in self.my_holdings_tfsa1 or t in self.my_holdings_tfsa2
                    )
                    if asset_names:
                        report += f"   → {asset_names} 수혜\n"

            for item in afterhours_summary.get('key_bearish', []):
                if isinstance(item, dict):
                    headline = item.get('headline', '')
                    assets = item.get('assets', [])
                else:
                    headline = item
                    assets = []
                report += f"🔴 {headline}\n"
                if assets:
                    asset_names = " · ".join(
                        f"{t}({self.ticker_names.get(t, t)})" for t in assets
                        if t in self.my_holdings_tfsa1 or t in self.my_holdings_tfsa2
                    )
                    if asset_names:
                        report += f"   → {asset_names} 직격\n"

            report += "=" * 37 + "\n"

        # TFSA 1
        tfsa1_actions = recommendations['tfsa1']
        sells = [a for a in tfsa1_actions if a['action'] == 'SELL']
        buys = [a for a in tfsa1_actions if a['action'] == 'BUY']

        existing_cash = self.accumulated_cash
        sell_total = sum(s['value'] for s in sells)

        report += "💡 오늘 추천\n"
        report += "=" * 37 + "\n"

        if not sells and not buys:
            report += "💡 TFSA 1\n→ 변경 없음\n"
        else:
            report += "💡 TFSA 1\n"

            for s in sells:
                name = self.ticker_names.get(s['ticker'], s['ticker'])
                type_label = "전량" if s['type'] == 'full' else "절반" if s['type'] == 'half' else "부분"
                report += f"\n📤 {type_label} 매도\n{s['ticker']} ({name})\n{s['shares']}주 @${s['price']:.2f} = ${s['value']:.2f}\n"

            # IMP-011: 매수 포맷 통일
            if buys:
                total_available = existing_cash + sell_total
                if sell_total > 0 and existing_cash > 0:
                    report += f"\n💰 매수가능: ${total_available:.0f} (현금 ${existing_cash:.0f} + 매도 ${sell_total:.0f})\n"
                elif sell_total > 0:
                    report += f"\n💰 매수가능: ${total_available:.0f}\n"
                else:
                    report += f"\n💰 매수가능: ${existing_cash:.0f}\n"

                for b in buys:
                    name = self.ticker_names.get(b['ticker'], b['ticker'])
                    report += f"{b['ticker']} ({name})\n{b['shares']}주 @${b['price']:.2f} = ${b['value']:.2f}  ({b['expected_pct']:+.1f}% 예상)\n"

        report += "=" * 37 + "\n"

        # IMP-013: TFSA 2 항상 표시
        report += "💡 TFSA 2\n"
        tfsa2_has_action = any(
            any(a['action'] != 'HOLD' for a in data['actions'])
            for data in recommendations['tfsa2'].values()
        )

        if not tfsa2_has_action:
            report += "→ 전체 유지\n"
        else:
            for ticker, data in recommendations['tfsa2'].items():
                actions = data['actions']
                if all(a['action'] == 'HOLD' for a in actions):
                    name = self.ticker_names.get(ticker, ticker)
                    purpose = data['purpose']
                    purpose_label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                    report += f"{ticker} ({name}) | {purpose_label}: 유지\n"
                    continue

                purpose = data['purpose']
                purpose_label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                name = self.ticker_names.get(ticker, ticker)
                report += f"\n{ticker} ({name}) | {purpose_label}\n"

                sell_val = 0
                for action in actions:
                    if action['action'] == 'SELL':
                        report += f"📤 전량 매도\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                        sell_val = action['value']
                    elif action['action'] == 'BUY':
                        buy_name = self.ticker_names.get(action['ticker'], action['ticker'])
                        report += f"💰 매수가능: ${sell_val:.0f}\n"
                        report += f"{action['ticker']} ({buy_name})\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}  ({action['expected_pct']:+.1f}% 예상)\n"
                        sell_val = 0

        return report

    # --------------------------------------------------------
    # 텔레그램 전송 (버튼 포함)
    # --------------------------------------------------------
    async def send_telegram(self, message, with_buttons=False, pending_trades=None):
        bot = Bot(token=self.telegram_token)
        try:
            if with_buttons and pending_trades:
                with open('pending_trades.json', 'w') as f:
                    json.dump(pending_trades, f)

                keyboard = [[
                    InlineKeyboardButton("✅ 완료", callback_data="trade_complete"),
                    InlineKeyboardButton("👀 관망", callback_data="trade_watch"),
                    InlineKeyboardButton("❌ 무시", callback_data="trade_ignore")
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await bot.send_message(
                    chat_id=int(self.telegram_chat_id),
                    text=message[:4000],
                    reply_markup=reply_markup
                )
            else:
                await bot.send_message(
                    chat_id=int(self.telegram_chat_id),
                    text=message[:4000]
                )
            print("✅ 텔레그램 전송 완료")
        except Exception as e:
            print(f"❌ 텔레그램 오류: {e}")

    # --------------------------------------------------------
    # 메인 실행
    # --------------------------------------------------------
    def run(self):
        try:
            print("\n" + "=" * 50)
            print("🤖 Daily Digest v4.0 시작")
            print("=" * 50)

            afterhours_news = self.load_afterhours_news()
            afterhours_summary = self.analyze_afterhours_summary(afterhours_news) if afterhours_news else None

            all_news = self.collect_all_news()
            if not all_news:
                asyncio.run(self.send_telegram("📭 오늘은 관련 뉴스가 없습니다."))
                return

            asset_impacts = self.aggregate_asset_impacts(all_news)
            rankings = self.create_rankings(asset_impacts)
            recommendations = self.generate_recommendations(rankings)

            report = self.format_telegram_report(recommendations, afterhours_summary)

            pending_trades = {
                'tfsa1': recommendations['tfsa1'],
                'tfsa2': recommendations['tfsa2'],
                'timestamp': self.now.isoformat()
            }

            asyncio.run(self.send_telegram(report, with_buttons=True, pending_trades=pending_trades))

            if afterhours_news:
                self.clear_afterhours_news()

            print("\n✅ 완료!")

        except Exception as e:
            error_msg = f"⚠️ Daily Digest 오류\n🕐 {datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M EST')}\n❌ {str(e)}\nGitHub Actions에서 로그를 확인하세요."
            try:
                asyncio.run(self.send_telegram(error_msg))
            except:
                pass
            raise


if __name__ == "__main__":
    agent = DailyDigest()
    agent.run()
