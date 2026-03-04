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
    """
    아침 종합 브리핑 (v3.0)
    전체 뉴스 수집 → AI 영향 분석 → 기술적 분석 → 순위표 → 구체적 추천
    """
    
    def __init__(self):
        print("🚀 Daily Digest v3.0 초기화...")
        
        # API 키
        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        
        # Groq 클라이언트
        self.groq = Groq(api_key=self.groq_api_key)
        
        # 시간대
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        
        # 포트폴리오 로드
        self.load_portfolio()
        
        # 모든 추적 자산
        self.all_tracked_assets = self.build_all_assets_list()
        
        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"📊 추적 자산: {len(self.all_tracked_assets)}개")
    
    def load_portfolio(self):
        """현재 포트폴리오 로드"""
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
                self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
                self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
                print(f"📂 current_portfolio.json 로드")
        except FileNotFoundError:
            print(f"📂 portfolio.yaml에서 초기화...")
            with open('portfolio.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            self.my_holdings_tfsa1 = {}
            for asset in config.get('tfsa1_assets', []):
                self.my_holdings_tfsa1[asset['ticker']] = asset['amount']
            
            self.my_holdings_tfsa2 = {}
            for asset in config.get('tfsa2_assets', []):
                self.my_holdings_tfsa2[asset['ticker']] = asset['amount']
            
            self.save_portfolio({
                'tfsa1': self.my_holdings_tfsa1,
                'tfsa2': self.my_holdings_tfsa2
            })
        
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        self.alternative_assets = [alt['ticker'] for alt in config.get('alternative_assets', [])]
        self.safe_assets = [safe['ticker'] for safe in config.get('safe_assets', [])]
        self.cost_settings = config.get('cost_settings', {})
        
        print(f"💼 TFSA 1: {list(self.my_holdings_tfsa1.keys())}")
        print(f"💰 TFSA 2: {list(self.my_holdings_tfsa2.keys())}")
    
    def build_all_assets_list(self):
        """분석할 모든 자산"""
        all_assets = set()
        all_assets.update(self.my_holdings_tfsa1.keys())
        all_assets.update(self.my_holdings_tfsa2.keys())
        all_assets.update(self.alternative_assets)
        all_assets.update(self.safe_assets)
        return list(all_assets)
    
    def save_portfolio(self, portfolio_data):
        """포트폴리오 저장"""
        portfolio_data['date'] = self.now.strftime('%Y-%m-%d')
        portfolio_data['time'] = self.now.strftime('%H:%M')
        
        with open('current_portfolio.json', 'w', encoding='utf-8') as f:
            json.dump(portfolio_data, f, indent=2)
        
        print(f"💾 포트폴리오 업데이트 완료")
    
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
                model="llama-3.1-70b-versatile",
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
                    magnitude = data.get('magnitude', 0)
                    confidence = data.get('confidence', 50) / 100
                    
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
        """기술적 분석"""
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period='60d')
            
            if len(df) < 20:
                return {'signal': 'neutral', 'reason': 'Insufficient data'}
            
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
        """순위표 생성"""
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
        """TFSA 1 추천"""
        actions = []
        
        for ticker, rank in my_ranks.items():
            if rank['weighted_score'] < -2:
                amount = self.my_holdings_tfsa1[ticker]
                actions.append({
                    'action': 'SELL',
                    'ticker': ticker,
                    'amount': amount,
                    'reason': f"Weighted score: {rank['weighted_score']:.1f}",
                    'urgency': 'HIGH'
                })
        
        top_assets = [r for r in all_rankings[:5] if r['weighted_score'] > 5]
        
        for asset in top_assets:
            if asset['ticker'] not in self.my_holdings_tfsa1:
                actions.append({
                    'action': 'BUY',
                    'ticker': asset['ticker'],
                    'score': asset['weighted_score'],
                    'reason': f"Expected: +{asset['net_return']*100:.1f}%, Confidence: {asset['confidence']}%"
                })
        
        return actions
    
    def generate_tfsa2_recommendations(self, my_ranks, all_rankings):
        """TFSA 2 추천"""
        safe_rankings = [r for r in all_rankings if r['ticker'] in self.safe_assets]
        safe_rankings.sort(key=lambda x: x['weighted_score'], reverse=True)
        
        if not safe_rankings:
            return []
        
        best_safe = safe_rankings[0]
        current = list(self.my_holdings_tfsa2.keys())[0] if self.my_holdings_tfsa2 else None
        
        if current and current != best_safe['ticker']:
            return [{
                'action': 'SWITCH',
                'from': current,
                'to': best_safe['ticker'],
                'reason': f"Better safe asset: +{best_safe['net_return']*100:.1f}%"
            }]
        
        return []
    
    def format_telegram_report(self, recommendations):
        """텔레그램 리포트 생성"""
        report = f"📊 일일 브리핑\n"
        report += f"🕐 {self.now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        report += "="*40 + "\n\n"
        
        tfsa1 = recommendations['tfsa1']
        if tfsa1:
            sells = [a for a in tfsa1 if a['action'] == 'SELL']
            buys = [a for a in tfsa1 if a['action'] == 'BUY']
            
            if sells:
                report += "🚨 TFSA 1 매도\n\n"
                for sell in sells:
                    report += f"매도: {sell['ticker']} ${sell['amount']:.0f}\n"
                    report += f"이유: {sell['reason']}\n\n"
            
            if buys:
                report += "💰 TFSA 1 매수\n\n"
                for buy in buys[:3]:
                    report += f"매수: {buy['ticker']}\n"
                    report += f"이유: {buy['reason']}\n\n"
        
        tfsa2 = recommendations['tfsa2']
        if tfsa2:
            report += "💰 TFSA 2 전환\n\n"
            for rec in tfsa2:
                report += f"전환: {rec['from']} → {rec['to']}\n"
                report += f"이유: {rec['reason']}\n\n"
        
        if not tfsa1 and not tfsa2:
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
        report = self.format_telegram_report(recommendations)
        
        asyncio.run(self.send_telegram(report))
        
        print("\n✅ 완료!")


if __name__ == "__main__":
    agent = DailyDigest()
    agent.run()
