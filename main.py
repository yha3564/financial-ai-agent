import os
import yaml
import requests
import json
from datetime import datetime
import google.generativeai as genai
from telegram import Bot
import asyncio
import pytz

class FinancialAgent:
    def __init__(self):
        # API 키 로드
        self.news_api_key = os.environ['NEWS_API_KEY']
        self.gemini_api_key = os.environ['GEMINI_API_KEY']
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        self.github_token = os.environ.get('GITHUB_TOKEN', '')
        
        # Gemini 설정
        genai.configure(api_key=self.gemini_api_key)
        self.model = genai.GenerativeModel('gemini-pro')
        
        # 포트폴리오 로드
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # 토론토 시간대
        self.toronto_tz = pytz.timezone('America/Toronto')
    
    def fetch_news(self):
        """뉴스 수집"""
        all_news = []
        
        # TFSA1 자산
        for asset in self.config['tfsa1_assets']:
            news = self._fetch_news_for_ticker(asset['ticker'], asset['name'], 'TFSA1')
            all_news.extend(news)
        
        # TFSA2 자산
        for asset in self.config['tfsa2_assets']:
            news = self._fetch_news_for_ticker(asset['ticker'], asset['name'], 'TFSA2')
            all_news.extend(news)
        
        return all_news
    
    def _fetch_news_for_ticker(self, ticker, name, account):
        """개별 종목 뉴스 수집"""
        # 티커에서 .TO 제거 (News API 검색용)
        search_ticker = ticker.replace('.TO', '')
        
        url = "https://newsapi.org/v2/everything"
        params = {
            'q': search_ticker,
            'language': 'en',
            'sortBy': 'publishedAt',
            'apiKey': self.news_api_key,
            'pageSize': 3,
            'from': datetime.now().strftime('%Y-%m-%d')
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                articles = response.json().get('articles', [])
                return [{
                    'account': account,
                    'asset': name,
                    'ticker': ticker,
                    'title': article['title'],
                    'description': article.get('description', ''),
                    'url': article['url'],
                    'published': article['publishedAt']
                } for article in articles]
        except Exception as e:
            print(f"뉴스 수집 오류 ({ticker}): {e}")
        
        return []
    
    def analyze_with_ai(self, news_item):
        """AI로 뉴스 분석"""
        account = news_item['account']
        
        prompt = f"""
당신은 전문 금융 애널리스트입니다. 다음 뉴스를 분석해주세요:

자산: {news_item['asset']} ({news_item['ticker']})
계좌: {account}
제목: {news_item['title']}
내용: {news_item['description']}

다음 형식으로 답변:
판단: [매도/매수/중립]
신뢰도: [0-100]
예상수익: [% 숫자만]
이유: [한 문장]
"""
        
        try:
            response = self.model.generate_content(prompt)
            return self.parse_ai_response(response.text, news_item)
        except Exception as e:
            print(f"AI 분석 오류: {e}")
            return None
    
    def parse_ai_response(self, text, news_item):
        """AI 응답 파싱"""
        result = {
            'account': news_item['account'],
            'asset': news_item['asset'],
            'ticker': news_item['ticker'],
            'title': news_item['title'],
            'url': news_item['url'],
            'signal': 'HOLD',
            'confidence': 50,
            'expected_return': 0,
            'reason': ''
        }
        
        text_upper = text.upper()
        
        # 신호 판단
        if '매도' in text or 'SELL' in text_upper:
            result['signal'] = 'SELL'
        elif '매수' in text or 'BUY' in text_upper:
            result['signal'] = 'BUY'
        
        # 숫자 추출
        lines = text.split('\n')
        for line in lines:
            if '신뢰도' in line or 'confidence' in line.lower():
                numbers = [int(s) for s in line.split() if s.isdigit()]
                if numbers:
                    result['confidence'] = min(numbers[0], 100)
            
            if '예상수익' in line or 'expected' in line.lower():
                numbers = [int(s) for s in line.split() if s.isdigit()]
                if numbers:
                    result['expected_return'] = numbers[0] / 100
            
            if '이유' in line or 'reason' in line.lower():
                result['reason'] = line.split(':', 1)[-1].strip()
        
        return result
    
    def search_alternatives(self, signal_data):
        """대체 자산 검색"""
        if signal_data['signal'] != 'SELL':
            return []
        
        # 간단한 섹터 기반 대안
        alternatives = [
            {
                'name': 'VOO - S&P500 USD',
                'ticker': 'VOO',
                'currency': 'USD',
                'expected_return': 0.08,
                'mer': 0.0003
            },
            {
                'name': 'VFV - S&P500 CAD',
                'ticker': 'VFV.TO',
                'currency': 'CAD',
                'expected_return': 0.07,
                'mer': 0.0008
            },
            {
                'name': 'XLE - Energy Sector',
                'ticker': 'XLE',
                'currency': 'USD',
                'expected_return': 0.12,
                'mer': 0.001
            }
        ]
        
        return self.rank_alternatives(alternatives, 500)
    
    def rank_alternatives(self, alternatives, amount):
        """순수익 기준 순위 매기기"""
        fx_fee = self.config['cost_settings']['fx_fee']
        
        ranked = []
        for alt in alternatives:
            # 순수익 계산
            if alt['currency'] == 'USD':
                # 환전 비용
                invested = amount * (1 - fx_fee)
                expected_value = invested * (1 + alt['expected_return'])
                final_value = expected_value * (1 - fx_fee)
            else:
                invested = amount
                expected_value = invested * (1 + alt['expected_return'])
                final_value = expected_value
            
            # MER 차감
            mer_cost = final_value * (alt['mer'] / 12 * 3)  # 3개월 기준
            net_value = final_value - mer_cost
            net_return = (net_value - amount) / amount
            
            ranked.append({
                **alt,
                'net_return': net_return,
                'net_value': net_value,
                'invested': invested
            })
        
        # 순수익 높은 순 정렬
        return sorted(ranked, key=lambda x: x['net_return'], reverse=True)
    
    def generate_report(self, analyses):
        """리포트 생성"""
        toronto_time = datetime.now(self.toronto_tz).strftime('%Y-%m-%d %H:%M %Z')
        
        report = f"📊 **일일 금융 브리핑**\n"
        report += f"🕐 {toronto_time}\n"
        report += f"{'='*40}\n\n"
        
        # 매도 신호
        sells = [a for a in analyses if a and a['signal'] == 'SELL' and a['confidence'] >= 65]
        if sells:
            report += "🔴 **매도 + 재투자 추천**\n\n"
            for item in sells:
                report += f"**{item['asset']}** ({item['account']})\n"
                report += f"📰 {item['title']}\n"
                report += f"⚠️ {item['reason']}\n"
                report += f"신뢰도: {item['confidence']}%\n\n"
                
                # 대체 자산 추천
                alternatives = self.search_alternatives(item)
                if alternatives:
                    report += "💰 **순수익 기준 TOP 3**\n\n"
                    medals = ['🥇', '🥈', '🥉']
                    for i, alt in enumerate(alternatives[:3]):
                        medal = medals[i] if i < 3 else f"{i+1}위"
                        flag = "🇺🇸" if alt['currency'] == 'USD' else "🇨🇦"
                        report += f"{medal} {alt['name']} {flag}\n"
                        report += f"   순수익: +{alt['net_return']*100:.1f}%\n"
                        report += f"   (예상 +{alt['expected_return']*100:.0f}%, 비용 포함)\n\n"
                
                report += f"🔗 {item['url']}\n"
                report += f"{'-'*40}\n\n"
        
        # 매수 신호
        buys = [a for a in analyses if a and a['signal'] == 'BUY' and a['confidence'] >= 65]
        if buys:
            report += "🟢 **매수 기회**\n\n"
            for item in buys:
                report += f"**{item['asset']}** ({item['account']})\n"
                report += f"📰 {item['title']}\n"
                report += f"💚 {item['reason']}\n"
                report += f"신뢰도: {item['confidence']}%\n"
                report += f"🔗 {item['url']}\n\n"
        
        if not sells and not buys:
            report += "✅ **오늘은 특별한 시그널이 없습니다**\n"
            report += "포트폴리오를 그대로 유지하세요.\n"
        
        return report
    
    async def send_telegram(self, message):
        """텔레그램 전송"""
        bot = Bot(token=self.telegram_token)
        
        # 메시지가 너무 길면 분할
        max_length = 4000
        if len(message) <= max_length:
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=message,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
        else:
            # 분할 전송
            parts = [message[i:i+max_length] for i in range(0, len(message), max_length)]
            for part in parts:
                await bot.send_message(
                    chat_id=self.telegram_chat_id,
                    text=part,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
    
    def run(self):
        """메인 실행"""
        print("🤖 금융 AI 에이전트 시작...")
        print(f"시간: {datetime.now(self.toronto_tz).strftime('%Y-%m-%d %H:%M %Z')}")
        
        # 1. 뉴스 수집
        print("\n📰 뉴스 수집 중...")
        news_list = self.fetch_news()
        print(f"   → {len(news_list)}개 뉴스 발견")
        
        if not news_list:
            print("   ⚠️ 뉴스 없음")
            asyncio.run(self.send_telegram("📭 오늘은 관련 뉴스가 없습니다."))
            return
        
        # 2. AI 분석
        print("\n🧠 AI 분석 중...")
        analyses = []
        for news in news_list[:15]:  # 최대 15개만 (무료 한도 보호)
            result = self.analyze_with_ai(news)
            if result:
                analyses.append(result)
                print(f"   ✓ {result['asset']}: {result['signal']} ({result['confidence']}%)")
        
        # 3. 리포트 생성
        print("\n📝 리포트 생성 중...")
        report = self.generate_report(analyses)
        
        # 4. 텔레그램 전송
        print("\n📱 텔레그램 전송 중...")
        asyncio.run(self.send_telegram(report))
        
        print("\n✅ 완료!")

if __name__ == "__main__":
    agent = FinancialAgent()
    agent.run()
