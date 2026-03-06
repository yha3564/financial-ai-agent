import os
import yaml
import json
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz
import asyncio
import time
import pandas as pd
import pandas_ta_classic as ta
import numpy as np
import yfinance as yf
from groq import Groq
try:
    from google import genai as genai_new
    USE_NEW_GENAI = True
except ImportError:
    import google.generativeai as genai
    USE_NEW_GENAI = False
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


class IntradayMonitor:
    """장중 30분마다 새 뉴스 모니터링 v4.0"""

    def __init__(self):
        print("🔍 Intraday Monitor v4.0 초기화...")

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
                    self.gemini = genai.GenerativeModel('gemini-2.0-flash')
                print("✅ Gemini 초기화 완료")
            except Exception as e:
                print(f"⚠️ Gemini 초기화 실패: {e}")

        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        self.seen_file = 'seen_news.json'

        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.cost_settings = self.config.get('cost_settings', {})
        self.trading_rules = self.config.get('trading_rules', {})
        self.alert_threshold = self.config.get('ranking_rules', {}).get('alert_threshold', 0.15)

        self.mer_map = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.mer_map[asset['ticker']] = asset.get('mer', 0.003)

        self.alternative_assets = [a['ticker'] for a in self.config.get('alternative_assets', [])]
        self.safe_assets = [a['ticker'] for a in self.config.get('safe_assets', [])]

        self.load_portfolio()
        self.build_ticker_name_map()

        all_tickers = list(set(
            list(self.my_holdings_tfsa1.keys()) +
            list(self.my_holdings_tfsa2.keys()) +
            self.alternative_assets +
            self.safe_assets
        ))
        self.all_tracked_assets = all_tickers
        self._load_prices(all_tickers)

        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")

    # --------------------------------------------------------
    # 포트폴리오 로드 (읽기 전용)
    # --------------------------------------------------------
    def load_portfolio(self):
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
            self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
            self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
            self.accumulated_cash = portfolio.get('accumulated_cash', 0)
        except FileNotFoundError:
            self.my_holdings_tfsa1 = {}
            self.my_holdings_tfsa2 = {}
            self.accumulated_cash = 0
            for asset in self.config.get('tfsa1_assets', []):
                self.my_holdings_tfsa1[asset['ticker']] = {
                    'shares': asset.get('shares', 0),
                    'avg_price': asset.get('avg_price', 0)
                }
            for asset in self.config.get('tfsa2_assets', []):
                self.my_holdings_tfsa2[asset['ticker']] = {
                    'shares': asset.get('shares', 0),
                    'avg_price': asset.get('avg_price', 0),
                    'purpose': asset.get('purpose', '')
                }

        for asset in self.config.get('tfsa2_assets', []):
            ticker = asset['ticker']
            if ticker in self.my_holdings_tfsa2:
                self.my_holdings_tfsa2[ticker]['purpose'] = asset.get('purpose', '')

        print(f"💼 TFSA1: {list(self.my_holdings_tfsa1.keys())}")
        print(f"💰 TFSA2: {list(self.my_holdings_tfsa2.keys())}")

    def build_ticker_name_map(self):
        self.ticker_names = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.ticker_names[asset['ticker']] = asset.get('name', asset['ticker'])

    # --------------------------------------------------------
    # 가격 배치 로드
    # --------------------------------------------------------
    def _load_prices(self, tickers):
        print(f"📥 가격 배치 로드 ({len(tickers)}개)...")
        self._prices = {}
        chunks = [tickers[i:i+20] for i in range(0, len(tickers), 20)]

        for i, chunk in enumerate(chunks):
            try:
                df = yf.download(" ".join(chunk), period="2d", auto_adjust=True, progress=False)
                if len(chunk) == 1:
                    ticker = chunk[0]
                    if 'Close' in df.columns and len(df) > 0:
                        self._prices[ticker] = float(df['Close'].iloc[-1])
                    else:
                        self._prices[ticker] = 0
                else:
                    if isinstance(df.columns, pd.MultiIndex):
                        for ticker in chunk:
                            try:
                                close = df['Close'][ticker].dropna()
                                self._prices[ticker] = float(close.iloc[-1]) if len(close) > 0 else 0
                            except:
                                self._prices[ticker] = 0
                    else:
                        for ticker in chunk:
                            self._prices[ticker] = 0
                if i < len(chunks) - 1:
                    time.sleep(1)
            except Exception as e:
                print(f"   ⚠️ 배치 오류: {e}")
                for ticker in chunk:
                    self._prices[ticker] = 0

        print(f"✅ 가격 로드 완료")

    def get_price(self, ticker):
        return self._prices.get(ticker, 0)

    def get_current_value(self, ticker, holding):
        shares = holding.get('shares', 0)
        price = self.get_price(ticker)
        return shares * price if price > 0 else 0

    # --------------------------------------------------------
    # seen_news 관리
    # --------------------------------------------------------
    def load_seen_news(self):
        try:
            with open(self.seen_file, 'r') as f:
                data = json.load(f)
            if data.get('date') != self.now.strftime('%Y-%m-%d'):
                return set()
            return set(data.get('seen_ids', []))
        except:
            return set()

    def save_seen_news(self, seen_ids):
        with open(self.seen_file, 'w') as f:
            json.dump({
                'date': self.now.strftime('%Y-%m-%d'),
                'seen_ids': list(seen_ids)
            }, f, indent=2)

    # --------------------------------------------------------
    # 뉴스 수집
    # --------------------------------------------------------
    def collect_recent_news(self):
        print("\n📰 최근 뉴스 수집 중...")
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
                                'content': ''
                            })
            except Exception as e:
                print(f"   ❌ NewsAPI {category}: {e}")

        rss_feeds = [
            ('Reuters', 'https://feeds.reuters.com/reuters/businessNews'),
            ('AP News', 'https://feeds.apnews.com/apnews/business'),
            ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex'),
            ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories'),
        ]
        for source, feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:15]:
                    url = entry.get('link', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        content = entry.get('summary', '')
                        if content:
                            content = BeautifulSoup(content, 'html.parser').get_text()[:500]
                        all_news.append({
                            'title': entry.get('title', ''),
                            'description': entry.get('summary', '')[:200],
                            'url': url,
                            'source': source,
                            'published': entry.get('published', ''),
                            'content': content
                        })
            except Exception as e:
                print(f"   ❌ RSS {source}: {e}")

        seen_ids = self.load_seen_news()
        new_news = [n for n in all_news if n['url'] not in seen_ids]

        print(f"✅ 전체 {len(all_news)}개 중 새 뉴스 {len(new_news)}개")

        crawl_count = 0
        for news in new_news:
            if not news['content'] and news['url'] and crawl_count < 20:
                try:
                    resp = requests.get(news['url'], timeout=5,
                                       headers={'User-Agent': 'Mozilla/5.0'})
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                            tag.decompose()
                        content = ' '.join([p.get_text() for p in soup.find_all('p')[:8]])[:600]
                        news['content'] = content
                        crawl_count += 1
                except:
                    pass

        return new_news, seen_ids

    # --------------------------------------------------------
    # AI 분석
    # --------------------------------------------------------
    def analyze_news_batch(self, news_batch):
        assets_str = ", ".join(self.all_tracked_assets)

        news_text = ""
        for i, n in enumerate(news_batch, 1):
            body = n.get('content') or n.get('description') or ''
            news_text += f"[{i}] {n['title']}\n{body[:300]}\n\n"

        prompt = f"""Analyze these {len(news_batch)} news articles for market impact.

NEWS:
{news_text}

ASSETS:
{assets_str}

Return JSON only:
{{
  "TICKER": {{"impact": "bullish/bearish/neutral", "magnitude": 0.XX, "confidence": XX, "reason": "brief"}},
  ...
}}

Rules:
- magnitude: 0.01~0.50
- confidence: 0~100
- Skip neutral/unaffected assets
- JSON only, no markdown"""

        for attempt in range(3):
            try:
                response = self.groq.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model="llama-3.3-70b-versatile",
                    temperature=0.3,
                    max_tokens=1500,
                )
                text = response.choices[0].message.content
                text = text.replace('```json', '').replace('```', '').strip()
                return json.loads(text)
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                    continue
                print(f"   ⚠️ Groq 3회 실패: {e}, Gemini 폴백...")

        if not self.gemini:
            return {}
        try:
            if USE_NEW_GENAI:
                response = self.gemini.models.generate_content(
                    model='gemini-2.0-flash', contents=prompt)
                text = response.text
            else:
                response = self.gemini.generate_content(prompt)
                text = response.text
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)
        except Exception as e:
            print(f"   ❌ Gemini 오류: {e}")
            return {}

    def aggregate_asset_impacts(self, new_news):
        print(f"\n🧠 AI 분석 중 (새 뉴스 {len(new_news)}개)...")

        asset_scores = {ticker: {'total': 0, 'confidences': [], 'reasons': [], 'count': 0}
                        for ticker in self.all_tracked_assets}

        batch_size = 10
        batches = [new_news[i:i+batch_size] for i in range(0, min(len(new_news), 50), batch_size)]

        for i, batch in enumerate(batches):
            print(f"   배치 [{i+1}/{len(batches)}]...")
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
                    'reasons': data['reasons'][:2],
                    'news_count': data['count']
                }
            else:
                results[ticker] = {
                    'magnitude': 0, 'confidence': 0,
                    'weighted_score': 0, 'reasons': [], 'news_count': 0
                }

        print(f"✅ 분석 완료")
        return results

    # --------------------------------------------------------
    # 기술적 분석
    # --------------------------------------------------------
    def technical_analysis(self, ticker):
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period='30d')
            if len(df) < 14:
                return {'signal': 'neutral', 'rsi': 50}

            df['rsi'] = ta.rsi(df['Close'], length=14)
            current_rsi = float(df['rsi'].iloc[-1])

            macd_df = ta.macd(df['Close'])
            df = df.join(macd_df)
            macd_bullish = df['MACD_12_26_9'].iloc[-1] > df['MACDs_12_26_9'].iloc[-1]

            if current_rsi < 30 and macd_bullish:
                return {'signal': 'buy', 'rsi': current_rsi}
            elif current_rsi > 70:
                return {'signal': 'sell', 'rsi': current_rsi}
            else:
                return {'signal': 'neutral', 'rsi': current_rsi}
        except:
            return {'signal': 'neutral', 'rsi': 50}

    # --------------------------------------------------------
    # 순위 + 알림 판단
    # --------------------------------------------------------
    def create_rankings(self, asset_impacts):
        fx_fee = self.cost_settings.get('fx_fee', 0.015)
        rankings = []

        for ticker in self.all_tracked_assets:
            impact = asset_impacts.get(ticker, {})
            weighted_score = impact.get('weighted_score', 0)

            if abs(weighted_score) < self.alert_threshold:
                continue

            mer = self.mer_map.get(ticker, 0.003)
            is_usd = not ticker.endswith('.TO')
            net_cost = (fx_fee * 2 if is_usd else 0) + (mer * 3 / 12)

            rankings.append({
                'ticker': ticker,
                'weighted_score': weighted_score,
                'magnitude': impact.get('magnitude', 0),
                'confidence': impact.get('confidence', 0),
                'net_cost': net_cost,
                'net_score': weighted_score - net_cost,
                'reasons': impact.get('reasons', []),
                'news_count': impact.get('news_count', 0)
            })

        rankings.sort(key=lambda x: abs(x['weighted_score']), reverse=True)
        return rankings

    # --------------------------------------------------------
    # 추천 생성
    # IMP-006/007: 모든 매도 후 즉시 대안자산 재투자 (현금 대기 금지)
    #              예외: 양수 점수 자산이 없을 때만 현금 보유 허용
    # IMP-002:     각 액션에 recommended_price / recommended_at 저장
    # --------------------------------------------------------
    def generate_recommendations(self, alert_rankings):
        tfsa1_rules = self.trading_rules.get('tfsa1', {})
        tfsa2_rules = self.trading_rules.get('tfsa2', {})
        concentration_threshold = tfsa1_rules.get('concentration_threshold', 0.10)
        recommended_at = self.now.isoformat()

        alert_map = {r['ticker']: r for r in alert_rankings}
        safe_rankings = [r for r in alert_rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['net_score'], reverse=True)

        # ── TFSA 1 ──
        tfsa1_actions = []
        available_cash = self.accumulated_cash
        partial_threshold = tfsa1_rules.get('partial_sell_threshold', 0.15)
        half_threshold = tfsa1_rules.get('half_sell_threshold', 0.25)
        full_threshold = tfsa1_rules.get('full_sell_threshold', 0.35)

        # STEP 1: 매도 판단
        for ticker, holding in self.my_holdings_tfsa1.items():
            if ticker not in alert_map:
                continue
            rank = alert_map[ticker]
            score = rank['weighted_score']
            price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            value = shares * price

            if score <= -full_threshold:
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': price, 'value': value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,      # IMP-002
                    'recommended_at': recommended_at  # IMP-002
                })
                available_cash += value

            elif score <= -half_threshold:
                sell_shares = round(shares * 0.5, 4)
                sell_value = sell_shares * price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'half',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                available_cash += sell_value

            elif score <= -partial_threshold:
                sell_pct = 0.3 + (abs(score) - partial_threshold) / (half_threshold - partial_threshold) * 0.2
                sell_shares = round(shares * sell_pct, 4)
                sell_value = sell_shares * price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'partial',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                available_cash += sell_value

        sold_tickers = [a['ticker'] for a in tfsa1_actions if a['action'] == 'SELL' and a['type'] == 'full']

        # STEP 2: 매수 후보 (alert_rankings + 전체 자산 중 양수)
        buy_candidates = [
            r for r in alert_rankings
            if r['weighted_score'] > self.alert_threshold
            and r['ticker'] not in sold_tickers
            and self.get_price(r['ticker']) > 0
        ]
        buy_candidates.sort(key=lambda x: x['net_score'], reverse=True)

        # IMP-006/007: 양수 후보 존재 여부 확인
        has_positive_candidate = len(buy_candidates) > 0

        # STEP 3: 현금/매도금 있으면 즉시 재투자
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
                    tfsa1_actions.append({
                        'action': 'BUY',
                        'ticker': candidate['ticker'],
                        'shares': round(per_amount / price, 4),
                        'price': price,
                        'value': per_amount,
                        'score': candidate['weighted_score'],
                        'expected_pct': candidate['magnitude'] * 100,
                        'recommended_price': price,
                        'recommended_at': recommended_at
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
                if t in alert_map:
                    held.append(alert_map[t])
                else:
                    held.append({
                        'ticker': t, 'weighted_score': 0, 'magnitude': 0,
                        'net_score': 0, 'net_cost': 0, 'reasons': [], 'news_count': 0
                    })
            if held:
                weakest = min(held, key=lambda x: x['weighted_score'])
                if top1['weighted_score'] - weakest['weighted_score'] >= self.alert_threshold:
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
                        tfsa1_actions.append({
                            'action': 'BUY',
                            'ticker': top1['ticker'],
                            'shares': round(total_buy / b_price, 4),
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

        for ticker, holding in self.my_holdings_tfsa2.items():
            purpose = holding.get('purpose', '')
            rank = alert_map.get(ticker, {})
            score = rank.get('weighted_score', 0) if rank else 0
            price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            value = shares * price
            actions = []

            best_alt = safe_rankings[0] if safe_rankings and safe_rankings[0]['ticker'] != ticker else None

            if score <= -full_threshold_t2 and best_alt:
                best_price = self.get_price(best_alt['ticker'])
                buy_shares = round(value / best_price, 4) if best_price > 0 else 0
                actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': price, 'value': value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                actions.append({
                    'action': 'BUY',
                    'ticker': best_alt['ticker'],
                    'shares': buy_shares,
                    'price': best_price,
                    'value': value,
                    'score': best_alt['weighted_score'],
                    'expected_pct': best_alt['magnitude'] * 100,
                    'recommended_price': best_price,
                    'recommended_at': recommended_at
                })
            else:
                actions.append({'action': 'HOLD', 'ticker': ticker})

            tfsa2_actions[ticker] = {
                'purpose': purpose,
                'current_value': value,
                'actions': actions
            }

        return {
            'tfsa1': tfsa1_actions,
            'tfsa2': tfsa2_actions,
            'available_cash': available_cash
        }

    # --------------------------------------------------------
    # 알림 메시지 포맷
    # IMP-005: 뉴스 헤드라인 아래 "   → 영향자산" 들여쓰기
    # IMP-011: 매수 섹션 "💰 매수가능" + 자산명 + 주수@가격
    # IMP-013: TFSA 2 항상 표시
    # --------------------------------------------------------
    def format_alert(self, alert_rankings, recommendations, top_news):
        now_str = self.now.strftime('%H:%M EST')
        msg = f"🚨 장중 알림 | {now_str}\n"
        msg += "=" * 37 + "\n"

        # IMP-005: 뉴스 + 영향자산 들여쓰기 구조
        bullish = [r for r in alert_rankings if r['weighted_score'] > 0]
        bearish = [r for r in alert_rankings if r['weighted_score'] < 0]

        # 전반적 방향
        if bullish or bearish:
            if len(bullish) > len(bearish):
                msg += f"🟢 전반적 호재 | 호재 {len(bullish)}건 | 악재 {len(bearish)}건\n"
            elif len(bearish) > len(bullish):
                msg += f"🔴 전반적 악재 | 악재 {len(bearish)}건 | 호재 {len(bullish)}건\n"
            else:
                msg += f"⚪ 혼재 | 호재 {len(bullish)}건 | 악재 {len(bearish)}건\n"

        # 뉴스 헤드라인 + 영향자산 들여쓰기
        if top_news:
            for news in top_news[:3]:
                title = news.get('title', '')[:70]
                # 이 뉴스와 관련된 alert_rankings 자산 찾기
                title_lower = title.lower()
                affected = [
                    r for r in alert_rankings
                    if r['ticker'].replace('.TO', '').lower() in title_lower
                    or any(w in title_lower for w in r.get('reasons', [''])[0].lower().split()[:3] if len(w) > 3)
                ]
                emoji = "🟢" if any(r['weighted_score'] > 0 for r in affected) else "🔴" if affected else "📰"
                msg += f"{emoji} {title}\n"
                if affected:
                    asset_str = " · ".join(
                        f"{r['ticker']}({self.ticker_names.get(r['ticker'], r['ticker'])}) {r['magnitude']*100:+.0f}%"
                        for r in affected[:3]
                    )
                    msg += f"   → {asset_str}\n"

        msg += "\n"

        # TFSA 1 추천
        tfsa1_actions = recommendations['tfsa1']
        sells = [a for a in tfsa1_actions if a['action'] == 'SELL']
        buys = [a for a in tfsa1_actions if a['action'] == 'BUY']
        sell_total = sum(s['value'] for s in sells)

        msg += "💡 TFSA 1\n"

        if not sells and not buys:
            msg += "→ 변경 없음\n"
        else:
            for s in sells:
                name = self.ticker_names.get(s['ticker'], s['ticker'])
                holding = self.my_holdings_tfsa1.get(s['ticker'], {})
                total_shares = holding.get('shares', 0)
                price = self.get_price(s['ticker'])
                total_value = total_shares * price
                type_label = "전량" if s['type'] == 'full' else "절반" if s['type'] == 'half' else "부분"
                pct = int(round((s['shares'] / total_shares * 100) if total_shares > 0 else 0))
                msg += f"\n📤 {type_label} 매도 ({pct}%)\n{s['ticker']} ({name})\n{s['shares']}주 @${price:.2f} = ${s['value']:.2f}\n"
                if s['type'] != 'full':
                    msg += f"잔여: {round(total_shares - s['shares'], 4)}주 계속 보유\n"

            # IMP-011: 매수 포맷 통일
            if buys:
                total_available = self.accumulated_cash + sell_total
                if sell_total > 0 and self.accumulated_cash > 0:
                    msg += f"\n💰 매수가능: ${total_available:.0f} (현금 ${self.accumulated_cash:.0f} + 매도 ${sell_total:.0f})\n"
                elif sell_total > 0:
                    msg += f"\n💰 매수가능: ${total_available:.0f}\n"
                else:
                    msg += f"\n💰 매수가능: ${self.accumulated_cash:.0f}\n"

                for b in buys:
                    name = self.ticker_names.get(b['ticker'], b['ticker'])
                    msg += f"{b['ticker']} ({name})\n{b['shares']}주 @${b['price']:.2f} = ${b['value']:.2f}  ({b['expected_pct']:+.1f}% 예상)\n"

        # IMP-013: TFSA 2 항상 표시
        msg += "\n💡 TFSA 2\n"
        tfsa2_has_action = any(
            any(a['action'] != 'HOLD' for a in data['actions'])
            for data in recommendations['tfsa2'].values()
        )

        if not tfsa2_has_action:
            msg += "→ 전체 유지\n"
        else:
            for ticker, data in recommendations['tfsa2'].items():
                actions = data['actions']
                if all(a['action'] == 'HOLD' for a in actions):
                    name = self.ticker_names.get(ticker, ticker)
                    purpose = data['purpose']
                    label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                    msg += f"{ticker} ({name}) | {label}: 유지\n"
                    continue

                purpose = data['purpose']
                label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                holding = self.my_holdings_tfsa2.get(ticker, {})
                price = self.get_price(ticker)
                shares = holding.get('shares', 0)
                value = shares * price
                name = self.ticker_names.get(ticker, ticker)
                msg += f"\n{ticker} ({name}) | {label}\n{shares}주 × ${price:.2f} = ${value:.2f}\n"

                sell_val = 0
                for action in actions:
                    if action['action'] == 'SELL':
                        msg += f"📤 전량 매도\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                        sell_val = action['value']
                    elif action['action'] == 'BUY':
                        buy_name = self.ticker_names.get(action['ticker'], action['ticker'])
                        msg += f"💰 매수가능: ${sell_val:.0f}\n"
                        msg += f"{action['ticker']} ({buy_name})\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                        sell_val = 0

        return msg

    # --------------------------------------------------------
    # 텔레그램 전송
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
                await bot.send_message(
                    chat_id=int(self.telegram_chat_id),
                    text=message[:4000],
                    reply_markup=InlineKeyboardMarkup(keyboard)
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
            print(f"🔍 장중 모니터링: {self.now.strftime('%H:%M %Z')}")
            print("=" * 50)

            new_news, seen_ids = self.collect_recent_news()

            if not new_news:
                print("✅ 새 뉴스 없음 - 종료")
                return

            impacts = self.aggregate_asset_impacts(new_news)
            alert_rankings = self.create_rankings(impacts)

            if not alert_rankings:
                print(f"✅ ±{self.alert_threshold} 이상 자산 없음 - 알림 생략")
                self.save_seen_news(seen_ids | {n['url'] for n in new_news})
                return

            print(f"🚨 알림 기준 초과 자산: {len(alert_rankings)}개")

            recommendations = self.generate_recommendations(alert_rankings)

            tfsa1_actions = recommendations['tfsa1']
            tfsa2_actions = recommendations['tfsa2']
            has_action = (
                any(a['action'] != 'HOLD' for a in tfsa1_actions) or
                any(a['action'] != 'HOLD' for data in tfsa2_actions.values() for a in data['actions'])
            )
            if not has_action:
                print("✅ 매수/매도 없음 - 알림 생략")
                self.save_seen_news(seen_ids | {n['url'] for n in new_news})
                return

            alert_tickers = [r['ticker'].replace('.TO', '').lower() for r in alert_rankings]
            def news_priority(n):
                title_lower = n.get('title', '').lower()
                return 0 if any(t in title_lower for t in alert_tickers) else 1
            top_news = sorted(new_news, key=news_priority)[:3]

            alert_msg = self.format_alert(alert_rankings, recommendations, top_news)

            pending_trades = {
                'tfsa1': recommendations['tfsa1'],
                'tfsa2': recommendations['tfsa2'],
                'timestamp': self.now.isoformat()
            }

            asyncio.run(self.send_telegram(alert_msg, with_buttons=True, pending_trades=pending_trades))

            self.save_seen_news(seen_ids | {n['url'] for n in new_news})
            print("\n✅ 완료!")

        except Exception as e:
            error_msg = f"⚠️ Intraday Monitor 오류\n🕐 {self.now.strftime('%H:%M EST')}\n❌ {str(e)}"
            try:
                asyncio.run(self.send_telegram(error_msg))
            except:
                pass
            raise


if __name__ == "__main__":
    monitor = IntradayMonitor()
    monitor.run()

        for asset in self.config.get('tfsa2_assets', []):
            ticker = asset['ticker']
            if ticker in self.my_holdings_tfsa2:
                self.my_holdings_tfsa2[ticker]['purpose'] = asset.get('purpose', '')

        print(f"💼 TFSA1: {list(self.my_holdings_tfsa1.keys())}")
        print(f"💰 TFSA2: {list(self.my_holdings_tfsa2.keys())}")

    def build_ticker_name_map(self):
        self.ticker_names = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.ticker_names[asset['ticker']] = asset.get('name', asset['ticker'])

    # --------------------------------------------------------
    # 가격 배치 로드
    # --------------------------------------------------------
    def _load_prices(self, tickers):
        print(f"📥 가격 배치 로드 ({len(tickers)}개)...")
        self._prices = {}
        chunks = [tickers[i:i+20] for i in range(0, len(tickers), 20)]

        for i, chunk in enumerate(chunks):
            try:
                df = yf.download(" ".join(chunk), period="2d", auto_adjust=True, progress=False)
                if len(chunk) == 1:
                    ticker = chunk[0]
                    if 'Close' in df.columns and len(df) > 0:
                        self._prices[ticker] = float(df['Close'].iloc[-1])
                    else:
                        self._prices[ticker] = 0
                else:
                    if isinstance(df.columns, pd.MultiIndex):
                        for ticker in chunk:
                            try:
                                close = df['Close'][ticker].dropna()
                                self._prices[ticker] = float(close.iloc[-1]) if len(close) > 0 else 0
                            except:
                                self._prices[ticker] = 0
                    else:
                        for ticker in chunk:
                            self._prices[ticker] = 0
                if i < len(chunks) - 1:
                    time.sleep(1)
            except Exception as e:
                print(f"   ⚠️ 배치 오류: {e}")
                for ticker in chunk:
                    self._prices[ticker] = 0

        print(f"✅ 가격 로드 완료")
        loaded = sum(1 for v in self._prices.values() if v > 0)
        if loaded == 0:
            raise RuntimeError(f"가격 다운로드 전부 실패 ({len(tickers)}개) - yfinance 또는 Yahoo 연결 문제")

    def get_price(self, ticker):
        return self._prices.get(ticker, 0)

    def get_current_value(self, ticker, holding):
        shares = holding.get('shares', 0)
        price = self.get_price(ticker)
        return shares * price if price > 0 else 0

    # --------------------------------------------------------
    # seen_news 관리
    # --------------------------------------------------------
    def load_seen_news(self):
        try:
            with open(self.seen_file, 'r') as f:
                data = json.load(f)
            if data.get('date') != self.now.strftime('%Y-%m-%d'):
                return set()
            return set(data.get('seen_ids', []))
        except:
            return set()

    def save_seen_news(self, seen_ids):
        with open(self.seen_file, 'w') as f:
            json.dump({
                'date': self.now.strftime('%Y-%m-%d'),
                'seen_ids': list(seen_ids)
            }, f, indent=2)

    # --------------------------------------------------------
    # 뉴스 수집
    # --------------------------------------------------------
    def collect_recent_news(self):
        print("\n📰 최근 뉴스 수집 중...")
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
                                'content': ''
                            })
            except Exception as e:
                print(f"   ❌ NewsAPI {category}: {e}")

        rss_feeds = [
            ('Reuters', 'https://feeds.reuters.com/reuters/businessNews'),
            ('AP News', 'https://feeds.apnews.com/apnews/business'),
            ('Yahoo Finance', 'https://finance.yahoo.com/news/rssindex'),
            ('MarketWatch', 'https://feeds.marketwatch.com/marketwatch/topstories'),
        ]
        for source, feed_url in rss_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:15]:
                    url = entry.get('link', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        content = entry.get('summary', '')
                        if content:
                            content = BeautifulSoup(content, 'html.parser').get_text()[:500]
                        all_news.append({
                            'title': entry.get('title', ''),
                            'description': entry.get('summary', '')[:200],
                            'url': url,
                            'source': source,
                            'published': entry.get('published', ''),
                            'content': content
                        })
            except Exception as e:
                print(f"   ❌ RSS {source}: {e}")

        seen_ids = self.load_seen_news()
        new_news = [n for n in all_news if n['url'] not in seen_ids]

        print(f"✅ 전체 {len(all_news)}개 중 새 뉴스 {len(new_news)}개")

        crawl_count = 0
        for news in new_news:
            if not news['content'] and news['url'] and crawl_count < 20:
                try:
                    resp = requests.get(news['url'], timeout=5,
                                       headers={'User-Agent': 'Mozilla/5.0'})
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, 'html.parser')
                        for tag in soup(['script', 'style', 'nav', 'header', 'footer']):
                            tag.decompose()
                        content = ' '.join([p.get_text() for p in soup.find_all('p')[:8]])[:600]
                        news['content'] = content
                        crawl_count += 1
                except:
                    pass

        return new_news, seen_ids

    # --------------------------------------------------------
    # AI 분석
    # --------------------------------------------------------
    def analyze_news_batch(self, news_batch):
        assets_str = ", ".join(self.all_tracked_assets)

        news_text = ""
        for i, n in enumerate(news_batch, 1):
            body = n.get('content') or n.get('description') or ''
            news_text += f"[{i}] {n['title']}\n{body[:300]}\n\n"

        prompt = f"""Analyze these {len(news_batch)} news articles for market impact.

NEWS:
{news_text}

ASSETS:
{assets_str}

Return JSON only:
{{
  "TICKER": {{"impact": "bullish/bearish/neutral", "magnitude": 0.XX, "confidence": XX, "reason": "brief"}},
  ...
}}

Rules:
- magnitude: 0.01~0.50
- confidence: 0~100
- Skip neutral/unaffected assets
- JSON only, no markdown"""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.3,
                max_tokens=1500,
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

    def aggregate_asset_impacts(self, new_news):
        print(f"\n🧠 AI 분석 중 (새 뉴스 {len(new_news)}개)...")

        asset_scores = {ticker: {'total': 0, 'confidences': [], 'reasons': [], 'count': 0}
                        for ticker in self.all_tracked_assets}

        batch_size = 10
        batches = [new_news[i:i+batch_size] for i in range(0, min(len(new_news), 50), batch_size)]

        for i, batch in enumerate(batches):
            print(f"   배치 [{i+1}/{len(batches)}]...")
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
                    'reasons': data['reasons'][:2],
                    'news_count': data['count']
                }
            else:
                results[ticker] = {
                    'magnitude': 0, 'confidence': 0,
                    'weighted_score': 0, 'reasons': [], 'news_count': 0
                }

        print(f"✅ 분석 완료")
        return results

    # --------------------------------------------------------
    # 기술적 분석
    # --------------------------------------------------------
    def technical_analysis(self, ticker):
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period='30d')
            if len(df) < 14:
                return {'signal': 'neutral', 'rsi': 50}

            df['rsi'] = ta.rsi(df['Close'], length=14)
            current_rsi = float(df['rsi'].iloc[-1])

            macd_df = ta.macd(df['Close'])
            df = df.join(macd_df)
            macd_bullish = df['MACD_12_26_9'].iloc[-1] > df['MACDs_12_26_9'].iloc[-1]

            if current_rsi < 30 and macd_bullish:
                return {'signal': 'buy', 'rsi': current_rsi}
            elif current_rsi > 70:
                return {'signal': 'sell', 'rsi': current_rsi}
            else:
                return {'signal': 'neutral', 'rsi': current_rsi}
        except:
            return {'signal': 'neutral', 'rsi': 50}

    # --------------------------------------------------------
    # 순위 + 알림 판단
    # --------------------------------------------------------
    def create_rankings(self, asset_impacts):
        fx_fee = self.cost_settings.get('fx_fee', 0.015)
        rankings = []

        for ticker in self.all_tracked_assets:
            impact = asset_impacts.get(ticker, {})
            weighted_score = impact.get('weighted_score', 0)

            if abs(weighted_score) < self.alert_threshold:
                continue

            mer = self.mer_map.get(ticker, 0.003)
            is_usd = not ticker.endswith('.TO')
            net_cost = (fx_fee * 2 if is_usd else 0) + (mer * 3 / 12)

            rankings.append({
                'ticker': ticker,
                'weighted_score': weighted_score,
                'magnitude': impact.get('magnitude', 0),
                'confidence': impact.get('confidence', 0),
                'net_cost': net_cost,
                'net_score': weighted_score - net_cost,
                'reasons': impact.get('reasons', []),
                'news_count': impact.get('news_count', 0)
            })

        rankings.sort(key=lambda x: abs(x['weighted_score']), reverse=True)
        return rankings

    # --------------------------------------------------------
    # 추천 생성
    # IMP-006/007: 모든 매도 후 즉시 대안자산 재투자 (현금 대기 금지)
    #              예외: 양수 점수 자산이 없을 때만 현금 보유 허용
    # IMP-002:     각 액션에 recommended_price / recommended_at 저장
    # --------------------------------------------------------
    def generate_recommendations(self, alert_rankings):
        tfsa1_rules = self.trading_rules.get('tfsa1', {})
        tfsa2_rules = self.trading_rules.get('tfsa2', {})
        concentration_threshold = tfsa1_rules.get('concentration_threshold', 0.10)
        recommended_at = self.now.isoformat()

        alert_map = {r['ticker']: r for r in alert_rankings}
        safe_rankings = [r for r in alert_rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['net_score'], reverse=True)

        # ── TFSA 1 ──
        tfsa1_actions = []
        available_cash = self.accumulated_cash
        partial_threshold = tfsa1_rules.get('partial_sell_threshold', 0.15)
        half_threshold = tfsa1_rules.get('half_sell_threshold', 0.25)
        full_threshold = tfsa1_rules.get('full_sell_threshold', 0.35)

        # STEP 1: 매도 판단
        for ticker, holding in self.my_holdings_tfsa1.items():
            if ticker not in alert_map:
                continue
            rank = alert_map[ticker]
            score = rank['weighted_score']
            price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            value = shares * price

            if score <= -full_threshold:
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': price, 'value': value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,      # IMP-002
                    'recommended_at': recommended_at  # IMP-002
                })
                available_cash += value

            elif score <= -half_threshold:
                sell_shares = round(shares * 0.5, 4)
                sell_value = sell_shares * price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'half',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                available_cash += sell_value

            elif score <= -partial_threshold:
                sell_pct = 0.3 + (abs(score) - partial_threshold) / (half_threshold - partial_threshold) * 0.2
                sell_shares = round(shares * sell_pct, 4)
                sell_value = sell_shares * price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'partial',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                available_cash += sell_value

        sold_tickers = [a['ticker'] for a in tfsa1_actions if a['action'] == 'SELL' and a['type'] == 'full']

        # STEP 2: 매수 후보 (alert_rankings + 전체 자산 중 양수)
        buy_candidates = [
            r for r in alert_rankings
            if r['weighted_score'] > self.alert_threshold
            and r['ticker'] not in sold_tickers
            and self.get_price(r['ticker']) > 0
        ]
        buy_candidates.sort(key=lambda x: x['net_score'], reverse=True)

        # IMP-006/007: 양수 후보 존재 여부 확인
        has_positive_candidate = len(buy_candidates) > 0

        # STEP 3: 현금/매도금 있으면 즉시 재투자
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
                    tfsa1_actions.append({
                        'action': 'BUY',
                        'ticker': candidate['ticker'],
                        'shares': round(per_amount / price, 4),
                        'price': price,
                        'value': per_amount,
                        'score': candidate['weighted_score'],
                        'expected_pct': candidate['magnitude'] * 100,
                        'recommended_price': price,
                        'recommended_at': recommended_at
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
                if t in alert_map:
                    held.append(alert_map[t])
                else:
                    held.append({
                        'ticker': t, 'weighted_score': 0, 'magnitude': 0,
                        'net_score': 0, 'net_cost': 0, 'reasons': [], 'news_count': 0
                    })
            if held:
                weakest = min(held, key=lambda x: x['weighted_score'])
                if top1['weighted_score'] - weakest['weighted_score'] >= self.alert_threshold:
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
                        tfsa1_actions.append({
                            'action': 'BUY',
                            'ticker': top1['ticker'],
                            'shares': round(total_buy / b_price, 4),
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

        for ticker, holding in self.my_holdings_tfsa2.items():
            purpose = holding.get('purpose', '')
            rank = alert_map.get(ticker, {})
            score = rank.get('weighted_score', 0) if rank else 0
            price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            value = shares * price
            actions = []

            best_alt = safe_rankings[0] if safe_rankings and safe_rankings[0]['ticker'] != ticker else None

            if score <= -full_threshold_t2 and best_alt:
                best_price = self.get_price(best_alt['ticker'])
                buy_shares = round(value / best_price, 4) if best_price > 0 else 0
                actions.append({
                    'action': 'SELL', 'type': 'full',
                    'ticker': ticker, 'shares': shares,
                    'price': price, 'value': value,
                    'score': score, 'expected_pct': score * 100,
                    'recommended_price': price,
                    'recommended_at': recommended_at
                })
                actions.append({
                    'action': 'BUY',
                    'ticker': best_alt['ticker'],
                    'shares': buy_shares,
                    'price': best_price,
                    'value': value,
                    'score': best_alt['weighted_score'],
                    'expected_pct': best_alt['magnitude'] * 100,
                    'recommended_price': best_price,
                    'recommended_at': recommended_at
                })
            else:
                actions.append({'action': 'HOLD', 'ticker': ticker})

            tfsa2_actions[ticker] = {
                'purpose': purpose,
                'current_value': value,
                'actions': actions
            }

        return {
            'tfsa1': tfsa1_actions,
            'tfsa2': tfsa2_actions,
            'available_cash': available_cash
        }

    # --------------------------------------------------------
    # 알림 메시지 포맷
    # IMP-005: 뉴스 헤드라인 아래 "   → 영향자산" 들여쓰기
    # IMP-011: 매수 섹션 "💰 매수가능" + 자산명 + 주수@가격
    # IMP-013: TFSA 2 항상 표시
    # --------------------------------------------------------
    def format_alert(self, alert_rankings, recommendations, top_news):
        now_str = self.now.strftime('%H:%M EST')
        msg = f"🚨 장중 알림 | {now_str}\n"
        msg += "=" * 37 + "\n"

        # IMP-005: 뉴스 + 영향자산 들여쓰기 구조
        bullish = [r for r in alert_rankings if r['weighted_score'] > 0]
        bearish = [r for r in alert_rankings if r['weighted_score'] < 0]

        # 전반적 방향
        if bullish or bearish:
            if len(bullish) > len(bearish):
                msg += f"🟢 전반적 호재 | 호재 {len(bullish)}건 | 악재 {len(bearish)}건\n"
            elif len(bearish) > len(bullish):
                msg += f"🔴 전반적 악재 | 악재 {len(bearish)}건 | 호재 {len(bullish)}건\n"
            else:
                msg += f"⚪ 혼재 | 호재 {len(bullish)}건 | 악재 {len(bearish)}건\n"

        # 뉴스 헤드라인 + 영향자산 들여쓰기
        if top_news:
            for news in top_news[:3]:
                title = news.get('title', '')[:70]
                # 이 뉴스와 관련된 alert_rankings 자산 찾기
                title_lower = title.lower()
                affected = [
                    r for r in alert_rankings
                    if r['ticker'].replace('.TO', '').lower() in title_lower
                    or any(w in title_lower for w in r.get('reasons', [''])[0].lower().split()[:3] if len(w) > 3)
                ]
                emoji = "🟢" if any(r['weighted_score'] > 0 for r in affected) else "🔴" if affected else "📰"
                msg += f"{emoji} {title}\n"
                if affected:
                    asset_str = " · ".join(
                        f"{r['ticker']}({self.ticker_names.get(r['ticker'], r['ticker'])}) {r['magnitude']*100:+.0f}%"
                        for r in affected[:3]
                    )
                    msg += f"   → {asset_str}\n"

        msg += "\n"

        # TFSA 1 추천
        tfsa1_actions = recommendations['tfsa1']
        sells = [a for a in tfsa1_actions if a['action'] == 'SELL']
        buys = [a for a in tfsa1_actions if a['action'] == 'BUY']
        sell_total = sum(s['value'] for s in sells)

        msg += "💡 TFSA 1\n"

        if not sells and not buys:
            msg += "→ 변경 없음\n"
        else:
            for s in sells:
                name = self.ticker_names.get(s['ticker'], s['ticker'])
                holding = self.my_holdings_tfsa1.get(s['ticker'], {})
                total_shares = holding.get('shares', 0)
                price = self.get_price(s['ticker'])
                total_value = total_shares * price
                type_label = "전량" if s['type'] == 'full' else "절반" if s['type'] == 'half' else "부분"
                pct = int(round((s['shares'] / total_shares * 100) if total_shares > 0 else 0))
                msg += f"\n📤 {type_label} 매도 ({pct}%)\n{s['ticker']} ({name})\n{s['shares']}주 @${price:.2f} = ${s['value']:.2f}\n"
                if s['type'] != 'full':
                    msg += f"잔여: {round(total_shares - s['shares'], 4)}주 계속 보유\n"

            # IMP-011: 매수 포맷 통일
            if buys:
                total_available = self.accumulated_cash + sell_total
                if sell_total > 0 and self.accumulated_cash > 0:
                    msg += f"\n💰 매수가능: ${total_available:.0f} (현금 ${self.accumulated_cash:.0f} + 매도 ${sell_total:.0f})\n"
                elif sell_total > 0:
                    msg += f"\n💰 매수가능: ${total_available:.0f}\n"
                else:
                    msg += f"\n💰 매수가능: ${self.accumulated_cash:.0f}\n"

                for b in buys:
                    name = self.ticker_names.get(b['ticker'], b['ticker'])
                    msg += f"{b['ticker']} ({name})\n{b['shares']}주 @${b['price']:.2f} = ${b['value']:.2f}  ({b['expected_pct']:+.1f}% 예상)\n"

        # IMP-013: TFSA 2 항상 표시
        msg += "\n💡 TFSA 2\n"
        tfsa2_has_action = any(
            any(a['action'] != 'HOLD' for a in data['actions'])
            for data in recommendations['tfsa2'].values()
        )

        if not tfsa2_has_action:
            msg += "→ 전체 유지\n"
        else:
            for ticker, data in recommendations['tfsa2'].items():
                actions = data['actions']
                if all(a['action'] == 'HOLD' for a in actions):
                    name = self.ticker_names.get(ticker, ticker)
                    purpose = data['purpose']
                    label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                    msg += f"{ticker} ({name}) | {label}: 유지\n"
                    continue

                purpose = data['purpose']
                label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
                holding = self.my_holdings_tfsa2.get(ticker, {})
                price = self.get_price(ticker)
                shares = holding.get('shares', 0)
                value = shares * price
                name = self.ticker_names.get(ticker, ticker)
                msg += f"\n{ticker} ({name}) | {label}\n{shares}주 × ${price:.2f} = ${value:.2f}\n"

                sell_val = 0
                for action in actions:
                    if action['action'] == 'SELL':
                        msg += f"📤 전량 매도\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                        sell_val = action['value']
                    elif action['action'] == 'BUY':
                        buy_name = self.ticker_names.get(action['ticker'], action['ticker'])
                        msg += f"💰 매수가능: ${sell_val:.0f}\n"
                        msg += f"{action['ticker']} ({buy_name})\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                        sell_val = 0

        return msg

    # --------------------------------------------------------
    # 텔레그램 전송
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
                await bot.send_message(
                    chat_id=int(self.telegram_chat_id),
                    text=message[:4000],
                    reply_markup=InlineKeyboardMarkup(keyboard)
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
            print(f"🔍 장중 모니터링: {self.now.strftime('%H:%M %Z')}")
            print("=" * 50)

            new_news, seen_ids = self.collect_recent_news()

            if not new_news:
                print("✅ 새 뉴스 없음 - 종료")
                return

            impacts = self.aggregate_asset_impacts(new_news)
            alert_rankings = self.create_rankings(impacts)

            if not alert_rankings:
                print(f"✅ ±{self.alert_threshold} 이상 자산 없음 - 알림 생략")
                self.save_seen_news(seen_ids | {n['url'] for n in new_news})
                return

            print(f"🚨 알림 기준 초과 자산: {len(alert_rankings)}개")

            recommendations = self.generate_recommendations(alert_rankings)

            tfsa1_actions = recommendations['tfsa1']
            tfsa2_actions = recommendations['tfsa2']
            has_action = (
                any(a['action'] != 'HOLD' for a in tfsa1_actions) or
                any(a['action'] != 'HOLD' for data in tfsa2_actions.values() for a in data['actions'])
            )
            if not has_action:
                print("✅ 매수/매도 없음 - 알림 생략")
                self.save_seen_news(seen_ids | {n['url'] for n in new_news})
                return

            alert_tickers = [r['ticker'].replace('.TO', '').lower() for r in alert_rankings]
            def news_priority(n):
                title_lower = n.get('title', '').lower()
                return 0 if any(t in title_lower for t in alert_tickers) else 1
            top_news = sorted(new_news, key=news_priority)[:3]

            alert_msg = self.format_alert(alert_rankings, recommendations, top_news)

            pending_trades = {
                'tfsa1': recommendations['tfsa1'],
                'tfsa2': recommendations['tfsa2'],
                'timestamp': self.now.isoformat()
            }

            asyncio.run(self.send_telegram(alert_msg, with_buttons=True, pending_trades=pending_trades))

            self.save_seen_news(seen_ids | {n['url'] for n in new_news})
            print("\n✅ 완료!")

        except Exception as e:
            error_msg = f"⚠️ Intraday Monitor 오류\n🕐 {self.now.strftime('%H:%M EST')}\n❌ {str(e)}"
            try:
                asyncio.run(self.send_telegram(error_msg))
            except:
                pass
            raise


if __name__ == "__main__":
    monitor = IntradayMonitor()
    monitor.run()
