import os
import yaml
import json
import requests
from datetime import datetime, timedelta
import pytz
import asyncio
from groq import Groq
from telegram import Bot

class IntradayMonitor:
    """장중 30분마다 새 뉴스 모니터링 (점수 ±5 이상만 알림)"""
    
    def __init__(self):
        print("🔍 Intraday Monitor 초기화...")
        
        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        
        self.groq = Groq(api_key=self.groq_api_key)
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        
        self.seen_file = 'seen_news.json'
        
        # 포트폴리오 로드
        self.load_portfolio()
        self.all_tracked_assets = self.build_all_assets_list()
        self.build_ticker_name_map()
        
        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
    
    def load_portfolio(self):
        """포트폴리오 로드 (읽기 전용)"""
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
                self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
                self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
                self.accumulated_cash = portfolio.get('accumulated_cash', 0)
                self.last_cash_added = portfolio.get('last_cash_added', '')
        except FileNotFoundError:
            with open('portfolio.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            self.my_holdings_tfsa1 = {}
            for asset in config.get('tfsa1_assets', []):
                self.my_holdings_tfsa1[asset['ticker']] = asset['amount']
            
            self.my_holdings_tfsa2 = {}
            for asset in config.get('tfsa2_assets', []):
                self.my_holdings_tfsa2[asset['ticker']] = asset['amount']
            
            self.accumulated_cash = 0
            self.last_cash_added = ''
        
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        self.alternative_assets = [alt['ticker'] for alt in config.get('alternative_assets', [])]
        self.safe_assets = [safe['ticker'] for safe in config.get('safe_assets', [])]
        self.cost_settings = config.get('cost_settings', {})
    
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
    
    def load_seen_news(self):
        """이미 본 뉴스 ID 로드"""
        try:
            with open(self.seen_file, 'r') as f:
                data = json.load(f)
                # 오늘 날짜가 아니면 초기화
                if data.get('date') != self.now.strftime('%Y-%m-%d'):
                    return set()
                return set(data.get('seen_ids', []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
    
    def save_seen_news(self, seen_ids):
        """본 뉴스 ID 저장"""
        data = {
            'date': self.now.strftime('%Y-%m-%d'),
            'seen_ids': list(seen_ids)
        }
        with open(self.seen_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def collect_recent_news(self):
        """최근 30분 뉴스만 수집"""
        print("\n📰 최근 30분 뉴스 수집 중...")
        
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
            except Exception as e:
                print(f"   ❌ {category} 수집 오류: {e}")
        
        # 최근 30분 뉴스만 필터링
        cutoff = self.now - timedelta(minutes=30)
        recent = []
        
        for news in all_news:
            try:
                published = datetime.strptime(
                    news['publishedAt'], 
                    '%Y-%m-%dT%H:%M:%SZ'
                ).replace(tzinfo=pytz.UTC)
                
                if published >= cutoff.astimezone(pytz.UTC):
                    recent.append(news)
            except:
                continue
        
        print(f"✅ 최근 30분 뉴스: {len(recent)}개")
        return recent
    
    def filter_new_news(self, all_news, seen_ids):
        """새 뉴스만 필터링"""
        new_news = []
        for news in all_news:
            news_id = news.get('url', '')
            if news_id and news_id not in seen_ids:
                new_news.append(news)
                seen_ids.add(news_id)
        
        print(f"✨ 새 뉴스: {len(new_news)}개")
        return new_news, seen_ids
    
    def analyze_news_impact(self, news_item):
        """뉴스 영향 분석"""
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
        """뉴스 영향 종합"""
        print("\n🧠 AI 뉴스 영향 분석 중...")
        
        asset_scores = {ticker: {'total': 0, 'confidence': [], 'count': 0} 
                       for ticker in self.all_tracked_assets}
        
        for i, news in enumerate(all_news[:20]):  # 장중은 최대 20개만
            print(f"   [{i+1}/{min(len(all_news), 20)}] {news['title'][:50]}...")
            
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
        """순위표 생성 (간소화)"""
        print("\n📊 순위표 생성 중...")
        
        rankings = []
        
        for ticker in self.all_tracked_assets:
            impact = asset_impacts.get(ticker, {})
            expected = impact.get('expected_return', 0)
            confidence = impact.get('confidence', 50)
            
            net_return = self.calculate_net_return(expected, ticker)
            weighted_score = net_return * (confidence / 100)
            
            rankings.append({
                'ticker': ticker,
                'expected_return': expected,
                'net_return': net_return,
                'confidence': confidence,
                'weighted_score': weighted_score,
                'news_count': impact.get('news_count', 0)
            })
        
        rankings.sort(key=lambda x: abs(x['weighted_score']), reverse=True)
        
        print(f"✅ 순위표 완료")
        return rankings
    
    def check_urgency(self, rankings):
        """긴급도 판단 (±5 이상만)"""
        urgent = []
        
        for rank in rankings:
            if abs(rank['weighted_score']) >= 5:
                urgent.append(rank)
        
        print(f"🚨 긴급 자산: {len(urgent)}개")
        return urgent
    
    def format_alert(self, new_news, urgent_rankings):
        """긴급 알림 메시지"""
        if not urgent_rankings:
            return None
        
        alert = f"🚨 장중 긴급 알림\n"
        alert += f"🕐 {self.now.strftime('%H:%M %Z')}\n"
        alert += "="*37 + "\n\n"
        alert += f"📰 새 뉴스: {len(new_news)}개\n\n"
        
        for rank in urgent_rankings[:5]:
            name = self.ticker_names.get(rank['ticker'], rank['ticker'])
            emoji = '🔴' if rank['weighted_score'] < 0 else '🟢'
            
            alert += f"{emoji} {rank['ticker']} ({name})\n"
            alert += f"점수: {rank['weighted_score']:.1f}\n"
            alert += f"예상: {rank['net_return']*100:+.1f}%\n"
            alert += f"신뢰도: {rank['confidence']}%\n"
            alert += f"관련 뉴스: {rank['news_count']}개\n\n"
        
        return alert
    
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
        print(f"🔍 장중 모니터링: {self.now.strftime('%H:%M %Z')}")
        print("="*50)
        
        # 1. 이미 본 뉴스 로드
        seen_ids = self.load_seen_news()
        print(f"📋 이미 본 뉴스: {len(seen_ids)}개")
        
        # 2. 최근 30분 뉴스
        recent_news = self.collect_recent_news()
        
        # 3. 새 뉴스만 필터링
        new_news, seen_ids = self.filter_new_news(recent_news, seen_ids)
        
        if not new_news:
            print("✅ 새 뉴스 없음 - 종료")
            return
        
        # 4. 새 뉴스만 분석
        impacts = self.aggregate_asset_impacts(new_news)
        rankings = self.create_rankings(impacts)
        
        # 5. 긴급도 판단
        urgent = self.check_urgency(rankings)
        
        # 6. 긴급하면 알림
        if urgent:
            alert = self.format_alert(new_news, urgent)
            if alert:
                asyncio.run(self.send_telegram(alert))
                print("✅ 긴급 알림 전송!")
        else:
            print("✅ 긴급 상황 없음 - 알림 생략")
        
        # 7. 본 뉴스 저장
        self.save_seen_news(seen_ids)
        
        print("\n✅ 완료!")


if __name__ == "__main__":
    monitor = IntradayMonitor()
    monitor.run()
