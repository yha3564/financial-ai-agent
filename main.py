import os
import yaml
import json
import requests
from datetime import datetime, timedelta
from groq import Groq
import asyncio
import pytz
import yfinance as yf
import pandas_ta_classic as ta
from telegram import Bot

class DailyDigest:
    """아침 종합 브리핑 v3.0 완전판"""
    
    def __init__(self):
        print("🚀 Daily Digest v3.0 초기화...")
        
        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        
        self.groq = Groq(api_key=self.groq_api_key)
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        
        self.load_portfolio()
        self.all_tracked_assets = self.build_all_assets_list()
        self.build_ticker_name_map()
        
        # 매월 1일 현금 입금 체크
        self.check_monthly_cash()
        
        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"📊 추적 자산: {len(self.all_tracked_assets)}개")
        print(f"💵 누적 현금: ${self.accumulated_cash:.0f}")
    
    def get_current_price(self, ticker):
        """현재 주가 조회"""
        try:
            stock = yf.Ticker(ticker)
            # 최근 가격 조회
            hist = stock.history(period='1d')
            if len(hist) > 0:
                return hist['Close'].iloc[-1]
            # 실패 시 info에서
            info = stock.info
            return info.get('currentPrice') or info.get('regularMarketPrice') or 0
        except:
            return 0
    
    def convert_old_portfolio_format(self, holdings):
        """기존 포맷을 새 포맷으로 변환"""
        converted = {}
        for ticker, value in holdings.items():
            if isinstance(value, dict):
                # 이미 새 포맷
                converted[ticker] = value
            else:
                # 기존 포맷 (숫자만) → 새 포맷으로 변환
                current_price = self.get_current_price(ticker)
                if current_price > 0:
                    shares = value / current_price
                else:
                    shares = 0
                
                converted[ticker] = {
                    'amount': value,
                    'shares': shares,
                    'purchase_price': current_price,
                    'purchase_date': self.now.strftime('%Y-%m-%d')
                }
        return converted
    
    def load_portfolio(self):
        """포트폴리오 로드"""
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
                
                # 기존 포맷 변환
                self.my_holdings_tfsa1 = self.convert_old_portfolio_format(
                    portfolio.get('tfsa1', {})
                )
                self.my_holdings_tfsa2 = self.convert_old_portfolio_format(
                    portfolio.get('tfsa2', {})
                )
                
                self.accumulated_cash = portfolio.get('accumulated_cash', 0)
                self.last_cash_added = portfolio.get('last_cash_added', '')
                print(f"📂 current_portfolio.json 로드")
        except FileNotFoundError:
            print(f"📂 portfolio.yaml에서 초기화...")
            with open('portfolio.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            self.my_holdings_tfsa1 = {}
            for asset in config.get('tfsa1_assets', []):
                ticker = asset['ticker']
                amount = asset['amount']
                shares = asset.get('shares', 0)  # shares 읽기
                price = self.get_current_price(ticker)
                
                # shares가 있으면 사용, 없으면 계산
                if shares == 0 and price > 0:
                    shares = amount / price
                
                self.my_holdings_tfsa1[ticker] = {
                    'amount': amount,
                    'shares': shares,
                    'purchase_price': price,
                    'purchase_date': self.now.strftime('%Y-%m-%d')
                }
            
            self.my_holdings_tfsa2 = {}
            for asset in config.get('tfsa2_assets', []):
                ticker = asset['ticker']
                amount = asset['amount']
                shares = asset.get('shares', 0)  # shares 읽기
                price = self.get_current_price(ticker)
                
                # shares가 있으면 사용, 없으면 계산
                if shares == 0 and price > 0:
                    shares = amount / price
                
                self.my_holdings_tfsa2[ticker] = {
                    'amount': amount,
                    'shares': shares,
                    'purchase_price': price,
                    'purchase_date': self.now.strftime('%Y-%m-%d')
                }
            
            # 첫 실행 시 초기 현금 설정
            monthly_cash_config = config.get('monthly_cash_inflow', {})
            self.accumulated_cash = monthly_cash_config.get('tfsa1', 0)
            self.last_cash_added = ''
            
            print(f"💵 첫 실행 - 초기 현금: ${self.accumulated_cash}")
            
            self.save_portfolio({
                'tfsa1': self.my_holdings_tfsa1,
                'tfsa2': self.my_holdings_tfsa2,
                'accumulated_cash': self.accumulated_cash,
                'last_cash_added': self.last_cash_added
            })
        
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        self.alternative_assets = [alt['ticker'] for alt in config.get('alternative_assets', [])]
        self.safe_assets = [safe['ticker'] for safe in config.get('safe_assets', [])]
        self.cost_settings = config.get('cost_settings', {})
        self.monthly_cash = config.get('monthly_cash_inflow', {})
        
        print(f"💼 TFSA 1: {list(self.my_holdings_tfsa1.keys())}")
        print(f"💰 TFSA 2: {list(self.my_holdings_tfsa2.keys())}")
    
    def check_monthly_cash(self):
        """매월 1일 현금 입금 체크"""
        today = self.now.strftime('%Y-%m-%d')
        
        # 오늘이 1일인가?
        if self.now.day == 1:
            # 이미 이번 달에 추가했는가?
            if self.last_cash_added != today:
                monthly_amount = self.monthly_cash.get('tfsa1', 0)
                self.accumulated_cash += monthly_amount
                self.last_cash_added = today
                print(f"💵 월간 현금 입금: +${monthly_amount} (총: ${self.accumulated_cash:.0f})")
                
                # 즉시 저장
                self.save_portfolio({
                    'tfsa1': self.my_holdings_tfsa1,
                    'tfsa2': self.my_holdings_tfsa2,
                    'accumulated_cash': self.accumulated_cash,
                    'last_cash_added': self.last_cash_added
                })
    
    def build_all_assets_list(self):
        """분석할 모든 자산"""
        all_assets = set()
        all_assets.update(self.my_holdings_tfsa1.keys())
        all_assets.update(self.my_holdings_tfsa2.keys())
        all_assets.update(self.alternative_assets)
        all_assets.update(self.safe_assets)
        return list(all_assets)
    
    def build_ticker_name_map(self):
        """티커 → 이름 매핑"""
        self.ticker_names = {}
        
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        for asset in config.get('tfsa1_assets', []):
            self.ticker_names[asset['ticker']] = asset['name']
        
        for asset in config.get('tfsa2_assets', []):
            self.ticker_names[asset['ticker']] = asset['name']
        
        for asset in config.get('alternative_assets', []):
            self.ticker_names[asset['ticker']] = asset['name']
        
        for asset in config.get('safe_assets', []):
            self.ticker_names[asset['ticker']] = asset['name']
    
    def save_portfolio(self, portfolio_data):
        """포트폴리오 저장"""
        portfolio_data['date'] = self.now.strftime('%Y-%m-%d')
        portfolio_data['time'] = self.now.strftime('%H:%M')
        
        with open('current_portfolio.json', 'w', encoding='utf-8') as f:
            json.dump(portfolio_data, f, indent=2)
        
        print(f"💾 포트폴리오 업데이트 완료")
    
    def calculate_current_value(self, ticker, holding_info):
        """현재 가치 계산"""
        if isinstance(holding_info, dict):
            shares = holding_info.get('shares', 0)
            current_price = self.get_current_price(ticker)
            return shares * current_price if current_price > 0 else holding_info.get('amount', 0)
        else:
            # 기존 포맷 (숫자만)
            return holding_info
    
    def collect_all_news(self):
        """전체 금융 뉴스 수집"""
        print("\n📰 전체 뉴스 수집 중...")
        
        all_news = []
        categories = ['business', 'technology']
        
        for category in categories:
            url = "https://newsapi.org/v2/top-headlines"
            params = {
                'language': 'en',
                'apiKey': self.news_api_key,
                'pageSize': 100,
                'category': category,
                'country': 'us'
            }
            
            try:
                response = requests.get(url, params=params, timeout=10)
                if response.status_code == 200:
                    articles = response.json().get('articles', [])
                    all_news.extend(articles)
                    print(f"   {category}: {len(articles)}개")
            except Exception as e:
                print(f"   ❌ {category} 수집 오류: {e}")
        
        print(f"✅ 총 {len(all_news)}개 뉴스 수집")
        return all_news[:150]
    
    def analyze_news_impact(self, news_item):
        """각 뉴스가 모든 자산에 미치는 영향 분석"""
        assets_str = ", ".join(self.all_tracked_assets)
        
        prompt = f"""News: {news_item['title']}
{news_item.get('description', '')}

Analyze impact on these assets:
{assets_str}

For EACH asset, determine:
1. Impact: bullish/bearish/neutral
2. Magnitude: estimated % change (0.01 to 0.50)
3. Confidence: 0-100

Return ONLY JSON format:
{{
  "TICKER": {{"impact": "bullish/bearish/neutral", "magnitude": 0.XX, "confidence": XX}},
  ...
}}

Be concise. Only include assets with significant impact."""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
                temperature=0.3,
                max_tokens=1000,
            )
            
            text = response.choices[0].message.content
            text = text.replace('```json', '').replace('```', '').strip()
            impacts = json.loads(text)
            return impacts
        
        except Exception as e:
            print(f"   ⚠️ AI 분석 오류: {e}")
            return {}
    
    def aggregate_asset_impacts(self, all_news):
        """모든 뉴스 영향을 자산별로 종합"""
        print("\n🧠 AI 뉴스 영향 분석 중...")
        
        asset_scores = {ticker: {'total': 0, 'confidence': [], 'count': 0} 
                       for ticker in self.all_tracked_assets}
        
        for i, news in enumerate(all_news[:50]):
            print(f"   [{i+1}/{min(len(all_news), 50)}] {news['title'][:50]}...")
            
            impacts = self.analyze_news_impact(news)
            
            for ticker, data in impacts.items():
                if ticker in asset_scores:
                    try:
                        magnitude = float(data.get('magnitude', 0))
                    except (ValueError, TypeError):
                        magnitude = 0
                    
                    try:
                        confidence = float(data.get('confidence', 50)) / 100
                    except (ValueError, TypeError):
                        confidence = 0.5
                    
                    if data.get('impact') == 'bearish':
                        magnitude = -magnitude
                    elif data.get('impact') == 'neutral':
                        magnitude = 0
                    
                    asset_scores[ticker]['total'] += magnitude
                    asset_scores[ticker]['confidence'].append(confidence)
                    asset_scores[ticker]['count'] += 1
        
        results = {}
        for ticker, data in asset_scores.items():
            if data['count'] > 0:
                avg_confidence = sum(data['confidence']) / len(data['confidence'])
                results[ticker] = {
                    'expected_return': data['total'],
                    'confidence': int(avg_confidence * 100),
                    'news_count': data['count']
                }
            else:
                results[ticker] = {
                    'expected_return': 0,
                    'confidence': 50,
                    'news_count': 0
                }
        
        print(f"✅ 자산별 영향 분석 완료")
        return results
    
    def technical_analysis(self, ticker):
        """기술적 분석 (완전판)"""
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period='60d')
            
            if len(df) < 20:
                return {'signal': 'neutral', 'reason': 'Insufficient data', 'rsi': 50}
            
            df['rsi'] = ta.rsi(df['Close'], length=14)
            current_rsi = df['rsi'].iloc[-1]
            
            macd = ta.macd(df['Close'])
            df = df.join(macd)
            
            df['ma_20'] = df['Close'].rolling(20).mean()
            df['ma_50'] = df['Close'].rolling(50).mean()
            
            signals = []
            
            if current_rsi < 30:
                signals.append('oversold')
            elif current_rsi > 70:
                signals.append('overbought')
            
            if len(df) > 1:
                if df['MACD_12_26_9'].iloc[-1] > df['MACDs_12_26_9'].iloc[-1]:
                    signals.append('bullish_macd')
                else:
                    signals.append('bearish_macd')
            
            if 'oversold' in signals and 'bullish_macd' in signals:
                return {'signal': 'buy', 'reason': f'RSI {current_rsi:.0f}, MACD bullish', 'rsi': current_rsi}
            elif 'overbought' in signals:
                return {'signal': 'sell', 'reason': f'RSI {current_rsi:.0f} overbought', 'rsi': current_rsi}
            else:
                return {'signal': 'neutral', 'reason': f'RSI {current_rsi:.0f}', 'rsi': current_rsi}
        
        except Exception as e:
            return {'signal': 'neutral', 'reason': 'Analysis failed', 'rsi': 50}
    
    def check_premarket_price(self, ticker):
        """선물/프리마켓 가격 확인"""
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='5d')
            
            if len(hist) < 2:
                return 0
            
            friday_close = hist['Close'].iloc[-2]
            
            try:
                current_price = stock.info.get('currentPrice') or stock.info.get('regularMarketPrice')
                if current_price:
                    change_pct = (current_price - friday_close) / friday_close
                    return change_pct
            except:
                pass
            
            return 0
        
        except:
            return 0
    
    def calculate_net_return(self, expected_return, ticker):
        """환전 비용 포함 순수익 계산"""
        fx_fee = self.cost_settings.get('fx_fee', 0.015)
        is_usd = not ticker.endswith('.TO')
        
        if is_usd:
            net_return = expected_return - (fx_fee * 2)
        else:
            net_return = expected_return
        
        mer = 0.003 * (3/12)
        net_return -= mer
        
        return net_return
    
    def create_rankings(self, asset_impacts):
        """순위표 생성 (완전판)"""
        print("\n📊 순위표 생성 중...")
        
        rankings = []
        
        for ticker in self.all_tracked_assets:
            impact = asset_impacts.get(ticker, {})
            expected = impact.get('expected_return', 0)
            confidence = impact.get('confidence', 50)
            
            already_moved = self.check_premarket_price(ticker)
            adjusted_expected = expected - already_moved
            
            tech = self.technical_analysis(ticker)
            
            if tech['signal'] == 'sell' and adjusted_expected > 0:
                adjusted_expected *= 0.7
            elif tech['signal'] == 'buy' and adjusted_expected < 0:
                adjusted_expected *= 0.7
            
            net_return = self.calculate_net_return(adjusted_expected, ticker)
            weighted_score = net_return * (confidence / 100)
            
            rankings.append({
                'ticker': ticker,
                'expected_return': expected,
                'already_moved': already_moved,
                'adjusted_expected': adjusted_expected,
                'net_return': net_return,
                'confidence': confidence,
                'weighted_score': weighted_score,
                'technical': tech,
                'news_count': impact.get('news_count', 0)
            })
        
        rankings.sort(key=lambda x: x['weighted_score'], reverse=True)
        
        print(f"✅ 순위표 완료 (총 {len(rankings)}개)")
        return rankings
    
    def generate_recommendations(self, rankings):
        """구체적 추천 생성"""
        print("\n💡 추천 생성 중...")
        
        my_tfsa1_ranks = {r['ticker']: r for r in rankings if r['ticker'] in self.my_holdings_tfsa1}
        my_tfsa2_ranks = {r['ticker']: r for r in rankings if r['ticker'] in self.my_holdings_tfsa2}
        
        tfsa1_recs = self.generate_tfsa1_recommendations(my_tfsa1_ranks, rankings)
        tfsa2_recs = self.generate_tfsa2_recommendations(my_tfsa2_ranks, rankings)
        
        return {
            'tfsa1': tfsa1_recs,
            'tfsa2': tfsa2_recs,
            'rankings': rankings[:10]
        }
    
    def generate_tfsa1_recommendations(self, my_ranks, all_rankings):
        """TFSA 1 추천 (AI 판단: 집중 vs 분산, 첫 실행/매월 1일 무조건 투자)"""
        available_cash = self.accumulated_cash
        actions = []
        
        # 매도: 점수 -2 이하만
        for ticker, rank in my_ranks.items():
            if rank['weighted_score'] < -2:
                holding = self.my_holdings_tfsa1[ticker]
                current_value = self.calculate_current_value(ticker, holding)
                available_cash += current_value
                actions.append({
                    'action': 'SELL',
                    'ticker': ticker,
                    'amount': current_value,
                    'score': rank['weighted_score'],
                    'reason': rank['technical']['reason']
                })
        
        # 매수 로직 (AI 판단)
        if available_cash > 0:
            # 무조건 투자 조건: 매월 1일 OR 포트폴리오 비어있음
            is_first_day = self.now.day == 1
            is_empty_portfolio = len(self.my_holdings_tfsa1) == 0
            
            if is_first_day or is_empty_portfolio:
                # 무조건 투자 (점수 무시)
                top_assets = [r for r in all_rankings[:10] 
                             if r['ticker'] not in self.my_holdings_tfsa1]
                print(f"   💡 무조건 투자 모드 (1일: {is_first_day}, 빈 포트폴리오: {is_empty_portfolio})")
            else:
                # 평일: 점수 +2 이상만
                top_assets = [r for r in all_rankings[:10] 
                             if r['weighted_score'] > 2 
                             and r['ticker'] not in self.my_holdings_tfsa1]
            
            # AI 판단: 집중 vs 분산
            if top_assets:
                top_3 = top_assets[:3]
                
                if len(top_3) >= 2:
                    score_diff = top_3[0]['weighted_score'] - top_3[1]['weighted_score']
                    
                    # 점수 차이로 판단
                    if score_diff >= 3.0:
                        # 1개 집중 (확실한 승자)
                        num_buys = 1
                        strategy = "집중"
                    elif score_diff >= 2.0:
                        # 2개 분산 (중간)
                        num_buys = 2
                        strategy = "분산(2)"
                    else:
                        # 3개 분산 (비슷함)
                        num_buys = 3
                        strategy = "분산(3)"
                else:
                    # 1개만 있으면 집중
                    num_buys = 1
                    strategy = "집중"
                
                # 매수 추천 생성
                for i in range(min(num_buys, len(top_3))):
                    actions.append({
                        'action': 'BUY',
                        'ticker': top_3[i]['ticker'],
                        'score': top_3[i]['weighted_score'],
                        'confidence': top_3[i]['confidence'],
                        'strategy': strategy  # 전략 표시
                    })
        
        # 현금 정보 (항상 표시)
        cash_source = []
        if self.accumulated_cash > 0:
            cash_source.append(f"${self.accumulated_cash:.0f} 누적")
        if available_cash > self.accumulated_cash:
            cash_source.append("매도금")
        
        # 무조건 투자 모드면 표시
        if self.now.day == 1 and self.accumulated_cash > 0:
            cash_source.append("(1일 즉시 투자)")
        elif len(self.my_holdings_tfsa1) == 0 and self.accumulated_cash > 0:
            cash_source.append("(첫 투자)")
        
        actions.append({
            'action': 'CASH_AVAILABLE',
            'amount': available_cash,
            'source': " + ".join(cash_source) if cash_source else "$0"
        })
        
        return actions
    
    def generate_tfsa2_recommendations(self, my_ranks, all_rankings):
        """TFSA 2 추천 (여러 자산 각각 전환, 점수 필터 +2)"""
        safe_rankings = [r for r in all_rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['weighted_score'], reverse=True)
        
        if not safe_rankings:
            return []
        
        best_safe = safe_rankings[0]
        
        # 점수 2 미만이면 추천 안 함
        if best_safe['weighted_score'] < 2:
            return []
        
        recommendations = []
        
        # 모든 TFSA 2 자산에 대해 검토
        for current_ticker in self.my_holdings_tfsa2.keys():
            # 이미 최선의 자산이면 스킵
            if current_ticker != best_safe['ticker']:
                recommendations.append({
                    'action': 'SWITCH',
                    'from': current_ticker,
                    'to': best_safe['ticker'],
                    'score': best_safe['weighted_score'],
                    'reason': f"+{best_safe['net_return']*100:.1f}% expected"
                })
        
        return recommendations
    
    def update_portfolio_from_recommendations(self, recommendations):
        """추천 기반 포트폴리오 자동 업데이트"""
        print("\n🔄 포트폴리오 자동 업데이트 중...")
        
        updated = False
        tfsa1 = recommendations['tfsa1']
        tfsa2 = recommendations['tfsa2']
        
        # TFSA 1 처리
        sells = [a for a in tfsa1 if a['action'] == 'SELL']
        buys = [a for a in tfsa1 if a['action'] == 'BUY']
        cash_info = [a for a in tfsa1 if a['action'] == 'CASH_AVAILABLE']
        
        # 매도 처리
        for sell in sells:
            if sell['ticker'] in self.my_holdings_tfsa1:
                holding = self.my_holdings_tfsa1[sell['ticker']]
                amount = self.calculate_current_value(sell['ticker'], holding)
                del self.my_holdings_tfsa1[sell['ticker']]
                self.accumulated_cash += amount
                print(f"   ✅ TFSA1에서 {sell['ticker']} 제거 (+${amount:.0f})")
                updated = True
        
        # 매수 처리 (AI 판단: 1개 or 2-3개)
        if buys and cash_info:
            available = cash_info[0]['amount']
            num_buys = len(buys)
            
            # 균등 분할
            for buy in buys:
                amount = available / num_buys
                ticker = buy['ticker']
                current_price = self.get_current_price(ticker)
                
                if current_price > 0:
                    shares = amount / current_price
                else:
                    shares = 0
                
                self.my_holdings_tfsa1[ticker] = {
                    'amount': amount,
                    'shares': shares,
                    'purchase_price': current_price,
                    'purchase_date': self.now.strftime('%Y-%m-%d')
                }
                
                self.accumulated_cash -= amount
                
                strategy = buy.get('strategy', '집중')
                print(f"   ✅ TFSA1에 {ticker} ${amount:.0f} 추가 ({shares:.4f}주, {strategy})")
                updated = True
        
        # TFSA 2 처리 (여러 자산 각각)
        for rec in tfsa2:
            if rec['action'] == 'SWITCH':
                # 기존 자산 제거
                if rec['from'] in self.my_holdings_tfsa2:
                    holding = self.my_holdings_tfsa2[rec['from']]
                    amount = self.calculate_current_value(rec['from'], holding)
                    del self.my_holdings_tfsa2[rec['from']]
                    print(f"   ✅ TFSA2에서 {rec['from']} 제거")
                    
                    # 새 자산 추가
                    ticker = rec['to']
                    current_price = self.get_current_price(ticker)
                    
                    if current_price > 0:
                        shares = amount / current_price
                    else:
                        shares = 0
                    
                    self.my_holdings_tfsa2[ticker] = {
                        'amount': amount,
                        'shares': shares,
                        'purchase_price': current_price,
                        'purchase_date': self.now.strftime('%Y-%m-%d')
                    }
                    
                    print(f"   ✅ TFSA2에 {ticker} ${amount:.0f} 추가 ({shares:.4f}주)")
                    updated = True
        
        # 파일 저장
        if updated or sells or buys:  # 현금 변동도 저장
            self.save_portfolio({
                'tfsa1': self.my_holdings_tfsa1,
                'tfsa2': self.my_holdings_tfsa2,
                'accumulated_cash': self.accumulated_cash,
                'last_cash_added': self.last_cash_added
            })
            print(f"✅ 포트폴리오 업데이트 완료! (누적 현금: ${self.accumulated_cash:.0f})")
        else:
            print("✅ 변경사항 없음")
    
    def format_telegram_report(self, recommendations):
        """텔레그램 리포트 생성"""
        report = f"📊 일일 브리핑\n"
        report += f"🕐 {self.now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        report += "="*37 + "\n\n"
        
        # 현재 포트폴리오 가치 표시
        if self.my_holdings_tfsa1:
            report += "💼 TFSA 1 현재 보유\n\n"
            for ticker, holding in self.my_holdings_tfsa1.items():
                name = self.ticker_names.get(ticker, ticker)
                current_value = self.calculate_current_value(ticker, holding)
                purchase_amount = holding.get('amount', 0) if isinstance(holding, dict) else holding
                
                if purchase_amount > 0:
                    profit_pct = ((current_value - purchase_amount) / purchase_amount) * 100
                    profit_text = f"{profit_pct:+.1f}%"
                else:
                    profit_text = "N/A"
                
                report += f"{ticker} ({name})\n"
                report += f"현재: ${current_value:.0f} ({profit_text})\n\n"
            
            report += "="*37 + "\n\n"
        
        tfsa1 = recommendations['tfsa1']
        sells = [a for a in tfsa1 if a['action'] == 'SELL']
        buys = [a for a in tfsa1 if a['action'] == 'BUY']
        cash_info = [a for a in tfsa1 if a['action'] == 'CASH_AVAILABLE']
        
        if sells:
            report += "🚨 TFSA 1 매도\n\n"
            for sell in sells:
                name = self.ticker_names.get(sell['ticker'], sell['ticker'])
                report += f"매도: {sell['ticker']} ({name})\n"
                report += f"금액: ${sell['amount']:.0f}\n\n"
            report += "="*37 + "\n\n"
        
        if cash_info:
            report += f"💵 사용 가능: ${cash_info[0]['amount']:.0f}\n"
            report += f"({cash_info[0]['source']})\n\n"
        
        if buys:
            strategy = buys[0].get('strategy', '집중')
            report += f"💰 매수 추천 ({strategy})\n\n"
            
            available = cash_info[0]['amount'] if cash_info else 0
            num_buys = len(buys)
            
            for i, buy in enumerate(buys, 1):
                name = self.ticker_names.get(buy['ticker'], buy['ticker'])
                suggested_amount = available / num_buys
                
                if num_buys == 1:
                    fraction_text = "(전액)"
                else:
                    fraction_text = f"(1/{num_buys})"
                
                if num_buys > 1:
                    report += f"{i}. "
                
                report += f"{buy['ticker']} ({name})\n"
                report += f"   금액: ${suggested_amount:.0f} {fraction_text}\n"
                report += f"   점수: {buy['score']:.1f}\n"
                report += f"   신뢰도: {buy['confidence']}%\n\n"
        
        # TFSA 2 간소화 버전 (여러 자산 표시)
        tfsa2 = recommendations['tfsa2']
        if tfsa2 or self.my_holdings_tfsa2:
            if sells or buys or self.my_holdings_tfsa1:
                report += "="*37 + "\n\n"
            
            report += "💰 TFSA 2\n\n"
            
            if tfsa2:
                # 전환 추천이 있는 경우
                for rec in tfsa2:
                    report += f"{rec['from']} → {rec['to']}\n"
            else:
                # 변경 없는 경우 - 모든 보유 자산 표시
                for ticker in self.my_holdings_tfsa2.keys():
                    name = self.ticker_names.get(ticker, ticker)
                    report += f"{ticker} ({name}) ✅\n"
        
        if not sells and not buys and not tfsa2 and not self.my_holdings_tfsa1:
            report += "✅ 오늘은 특별한 변경사항이 없습니다.\n"
        
        return report
    
    async def send_telegram(self, message):
        """텔레그램 전송"""
        bot = Bot(token=self.telegram_token)
        try:
            await bot.send_message(
                chat_id=int(self.telegram_chat_id),
                text=message[:4000]
            )
            print("✅ 텔레그램 전송 완료")
        except Exception as e:
            print(f"❌ 텔레그램 오류: {e}")
    
    def run(self):
        """메인 실행"""
        print("\n" + "="*50)
        print("🤖 Daily Digest v3.0 시작")
        print("="*50)
        
        all_news = self.collect_all_news()
        
        if not all_news:
            asyncio.run(self.send_telegram("📭 오늘은 관련 뉴스가 없습니다."))
            return
        
        asset_impacts = self.aggregate_asset_impacts(all_news)
        rankings = self.create_rankings(asset_impacts)
        recommendations = self.generate_recommendations(rankings)
        
        # 자동 업데이트
        self.update_portfolio_from_recommendations(recommendations)
        
        report = self.format_telegram_report(recommendations)
        asyncio.run(self.send_telegram(report))
        
        print("\n✅ 완료!")


if __name__ == "__main__":
    agent = DailyDigest()
    agent.run()
