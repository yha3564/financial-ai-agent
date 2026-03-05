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
import google.generativeai as genai
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


class IntradayMonitor:
    """장중 30분마다 새 뉴스 모니터링 v4.0"""

    def __init__(self):
        print("🔍 Intraday Monitor v4.0 초기화...")

        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.gemini_api_key = os.environ['GEMINI_API_KEY']
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']

        self.groq = Groq(api_key=self.groq_api_key)
        genai.configure(api_key=self.gemini_api_key)
        self.gemini = genai.GenerativeModel('gemini-1.5-flash')

        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        self.seen_file = 'seen_news.json'

        # 설정 로드
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.cost_settings = self.config.get('cost_settings', {})
        self.trading_rules = self.config.get('trading_rules', {})
        self.alert_threshold = self.config.get('ranking_rules', {}).get('alert_threshold', 0.15)

        # MER 맵
        self.mer_map = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.mer_map[asset['ticker']] = asset.get('mer', 0.003)

        self.alternative_assets = [a['ticker'] for a in self.config.get('alternative_assets', [])]
        self.safe_assets = [a['ticker'] for a in self.config.get('safe_assets', [])]

        self.load_portfolio()
        self.build_ticker_name_map()

        # 전체 자산 가격 배치 로드
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

        # purpose 보강
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
                    if isinstance(df.columns, pd.MultiIndex):
                        for ticker in chunk:
                            try:
                                close = df['Close'][ticker].dropna()
                                self._prices[ticker] = float(close.iloc[-1]) if len(close) > 0 else 0
                            except:
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
    # 뉴스 수집 (NewsAPI + RSS + 크롤링)
    # --------------------------------------------------------
    def collect_recent_news(self):
        print("\n📰 최근 뉴스 수집 중...")
        all_news = []
        seen_urls = set()

        # 1. NewsAPI
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

        # 2. RSS 피드
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

        # 3. 새 뉴스 필터링 (seen_ids)
        seen_ids = self.load_seen_news()
        new_news = []
        for n in all_news:
            if n['url'] not in seen_ids:
                new_news.append(n)

        print(f"✅ 전체 {len(all_news)}개 중 새 뉴스 {len(new_news)}개")

        # 4. 새 뉴스 본문 크롤링 (최대 20개)
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
    # AI 분석 (배치 + Gemini 폴백)
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

        # Groq 시도
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

        # Gemini 폴백
        try:
            response = self.gemini.generate_content(prompt)
            text = response.text.replace('```json', '').replace('```', '').strip()
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

            # 알림 기준 미만이면 스킵
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
    # 추천 생성 (장중)
    # --------------------------------------------------------
    def generate_recommendations(self, alert_rankings):
        """장중 알림용 추천 - portfolio 변경 없음"""
        tfsa1_rules = self.trading_rules.get('tfsa1', {})
        tfsa2_rules = self.trading_rules.get('tfsa2', {})
        concentration_threshold = tfsa1_rules.get('concentration_threshold', 0.10)

        alert_map = {r['ticker']: r for r in alert_rankings}
        safe_rankings = [r for r in alert_rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['net_score'], reverse=True)

        # ── TFSA 1 ──
        tfsa1_actions = []
        available_cash = self.accumulated_cash
        partial_threshold = tfsa1_rules.get('partial_sell_threshold', 0.15)
        half_threshold = tfsa1_rules.get('half_sell_threshold', 0.25)
        full_threshold = tfsa1_rules.get('full_sell_threshold', 0.35)

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
                    'score': score, 'expected_pct': score * 100
                })
                available_cash += value

            elif score <= -half_threshold:
                sell_shares = round(shares * 0.5, 4)
                sell_value = sell_shares * price
                tfsa1_actions.append({
                    'action': 'SELL', 'type': 'half',
                    'ticker': ticker, 'shares': sell_shares,
                    'price': price, 'value': sell_value,
                    'score': score, 'expected_pct': score * 100
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
                    'score': score, 'expected_pct': score * 100
                })
                available_cash += sell_value

        # 매수 후보
        sold_tickers = [a['ticker'] for a in tfsa1_actions if a['action'] == 'SELL' and a['type'] == 'full']
        sell_proceeds = available_cash - self.accumulated_cash

        buy_candidates = [r for r in alert_rankings
                          if r['weighted_score'] > self.alert_threshold
                          and r['ticker'] not in sold_tickers]
        buy_candidates.sort(key=lambda x: x['net_score'], reverse=True)

        # 매도 발생했는데 매수 후보 없으면 전체 자산 중 MER 낮은 순으로 강제 선정 (현금 보유 금지)
        if available_cash > 0 and not buy_candidates and sell_proceeds > 0:
            fx_fee = self.cost_settings.get('fx_fee', 0.015)
            buy_candidates = sorted(
                [{'ticker': t,
                  'weighted_score': 0,
                  'magnitude': 0,
                  'net_cost': (fx_fee * 2 if not t.endswith('.TO') else 0) + self.mer_map.get(t, 0.003) * 3 / 12,
                  'net_score': -(self.mer_map.get(t, 0.003))}
                 for t in self.all_tracked_assets
                 if self.get_price(t) > 0 and t not in sold_tickers],
                key=lambda x: x['net_score'], reverse=True
            )[:5]

        if available_cash > 0 and buy_candidates:
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
                        'expected_pct': candidate['magnitude'] * 100
                    })

        # ── TFSA 2 (목적별) ──
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
                    'score': score, 'expected_pct': score * 100
                })
                actions.append({
                    'action': 'BUY',
                    'ticker': best_alt['ticker'],
                    'shares': buy_shares,
                    'price': best_price,
                    'value': value,
                    'score': best_alt['weighted_score'],
                    'expected_pct': best_alt['magnitude'] * 100
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
    # --------------------------------------------------------
    def format_alert(self, alert_rankings, recommendations, top_news):
        now_str = self.now.strftime('%H:%M EST')
        msg = f"🚨 장중 알림 | {now_str}\n"
        msg += "=" * 37 + "\n"

        # 주요 뉴스
        if top_news:
            msg += f"\n📰 \"{top_news[0]['title'][:60]}\"\n"
            if len(top_news) > 1:
                msg += f"📰 \"{top_news[1]['title'][:60]}\"\n"

        # 영향 자산
        bullish = [r for r in alert_rankings if r['weighted_score'] > 0]
        bearish = [r for r in alert_rankings if r['weighted_score'] < 0]

        if bearish:
            msg += "\n🔴 영향 자산\n"
            msg += "=" * 37 + "\n"
            for r in bearish[:5]:
                name = self.ticker_names.get(r['ticker'], r['ticker'])
                msg += f"{r['ticker']} ({name})  {r['magnitude']*100:+.1f}% 예상\n"

        if bullish:
            msg += "\n🟢 영향 자산\n"
            msg += "=" * 37 + "\n"
            for r in bullish[:5]:
                name = self.ticker_names.get(r['ticker'], r['ticker'])
                msg += f"{r['ticker']} ({name})  {r['magnitude']*100:+.1f}% 예상\n"

        # TFSA 1 추천
        tfsa1_actions = recommendations['tfsa1']
        sells = [a for a in tfsa1_actions if a['action'] == 'SELL']
        buys = [a for a in tfsa1_actions if a['action'] == 'BUY']

        msg += "\n💡 TFSA 1\n"
        msg += "=" * 37 + "\n"
        msg += f"💵 보유 현금: ${self.accumulated_cash:.0f}\n"

        sell_total = sum(s['value'] for s in sells)

        for s in sells:
            name = self.ticker_names.get(s['ticker'], s['ticker'])
            holding = self.my_holdings_tfsa1.get(s['ticker'], {})
            total_shares = holding.get('shares', 0)
            price = self.get_price(s['ticker'])
            total_value = total_shares * price
            type_label = "전량" if s['type'] == 'full' else "절반" if s['type'] == 'half' else "부분"
            msg += f"\n{s['ticker']} ({name})\n"
            msg += f"{total_shares}주  ${price:.2f}  = ${total_value:.2f}\n"
            msg += f"📤 {type_label} 매도 {s['shares']}주\n"

        if sells or buys:
            total_available = self.accumulated_cash + sell_total
            if sell_total > 0:
                msg += f"\n💰 매수가능: ${total_available:.0f} (현금 ${self.accumulated_cash:.0f} + 매도 ${sell_total:.0f})\n"
            else:
                msg += f"\n💰 매수가능: ${total_available:.0f}\n"

        if buys:
            for b in buys:
                name = self.ticker_names.get(b['ticker'], b['ticker'])
                msg += f"\n{b['ticker']} ({name})\n"
                msg += f"📥 매수 {b['shares']}주 @${b['price']:.2f} = ${b['value']:.2f}\n"

        if not sells and not buys:
            msg += "→ 변경 없음\n"

        # TFSA 2 - HOLD면 섹션 생략
        for ticker, data in recommendations['tfsa2'].items():
            actions = data['actions']
            if all(a['action'] == 'HOLD' for a in actions):
                continue

            purpose = data['purpose']
            label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else purpose
            msg += f"\n💡 TFSA 2 | {label}\n"
            msg += "=" * 37 + "\n"

            holding = self.my_holdings_tfsa2.get(ticker, {})
            price = self.get_price(ticker)
            shares = holding.get('shares', 0)
            value = shares * price
            name = self.ticker_names.get(ticker, ticker)
            msg += f"{ticker} ({name})\n{shares}주  ${price:.2f}  = ${value:.2f}\n"

            for action in actions:
                if action['action'] == 'SELL':
                    msg += f"📤 전량 매도 {action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"
                elif action['action'] == 'BUY':
                    buy_name = self.ticker_names.get(action['ticker'], action['ticker'])
                    msg += f"📥 매수\n{action['ticker']} ({buy_name})\n{action['shares']}주 @${action['price']:.2f} = ${action['value']:.2f}\n"

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

            # 새 뉴스 수집
            new_news, seen_ids = self.collect_recent_news()

            if not new_news:
                print("✅ 새 뉴스 없음 - 종료")
                return

            # AI 분석
            impacts = self.aggregate_asset_impacts(new_news)
            alert_rankings = self.create_rankings(impacts)

            if not alert_rankings:
                print(f"✅ ±{self.alert_threshold} 이상 자산 없음 - 알림 생략")
                self.save_seen_news(seen_ids | {n['url'] for n in new_news})
                return

            print(f"🚨 알림 기준 초과 자산: {len(alert_rankings)}개")

            # 추천 생성
            recommendations = self.generate_recommendations(alert_rankings)

            # 알림 메시지
            top_news = sorted(new_news, key=lambda x: x.get('title', ''), reverse=True)[:3]
            alert_msg = self.format_alert(alert_rankings, recommendations, top_news)

            # pending_trades 저장
            pending_trades = {
                'tfsa1': recommendations['tfsa1'],
                'tfsa2': recommendations['tfsa2'],
                'timestamp': self.now.isoformat()
            }

            asyncio.run(self.send_telegram(alert_msg, with_buttons=True, pending_trades=pending_trades))

            # seen_news 업데이트
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
