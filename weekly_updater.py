import os
import yaml
import requests
from datetime import datetime, timedelta
from groq import Groq
import json
import pytz

class WeeklyUpdater:
    """
    매주 핫종목 자동 발견 & 업데이트
    암호화폐 제외
    """
    
    def __init__(self):
        print("🔄 Weekly Updater 시작...")
        
        self.news_api_key = os.environ['NEWS_API_KEY']
        self.groq_api_key = os.environ['GROQ_API_KEY']
        self.groq = Groq(api_key=self.groq_api_key)
        
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        
        # 암호화폐 관련 티커 (제외 목록)
        self.crypto_tickers = [
            'BTC', 'ETH', 'COIN', 'MARA', 'RIOT', 'MSTR',
            'BITF', 'HUT', 'CLSK', 'BTBT', 'SOS', 'CAN',
            'GBTC', 'ETHE', 'BITO', 'BKCH', 'IREN'
        ]
        
        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d')}")
    
    def collect_weekly_news(self):
        """지난 7일 뉴스 수집"""
        print("\n📰 지난 주 뉴스 수집 중...")
        
        cutoff = self.now - timedelta(days=7)
        from_date = cutoff.strftime('%Y-%m-%dT%H:%M:%S')
        
        url = "https://newsapi.org/v2/everything"
        params = {
            'language': 'en',
            'sortBy': 'publishedAt',
            'apiKey': self.news_api_key,
            'pageSize': 100,
            'from': from_date,
            'q': 'stock OR shares OR equity OR earnings'
        }
        
        try:
            response = requests.get(url, params=params, timeout=15)
            if response.status_code == 200:
                articles = response.json().get('articles', [])
                print(f"✅ {len(articles)}개 뉴스 수집")
                return articles
        except Exception as e:
            print(f"❌ 뉴스 수집 오류: {e}")
        
        return []
    
    def extract_hot_tickers(self, news_list):
        """뉴스에서 핫한 종목 추출 (crypto 제외)"""
        print("\n🔥 핫종목 추출 중...")
        
        # 뉴스 제목/설명 합치기
        all_text = "\n".join([
            f"{n['title']} {n.get('description', '')}" 
            for n in news_list[:50]
        ])
        
        prompt = f"""Analyze this week's financial news and extract stock tickers.

News summary:
{all_text[:3000]}

Rules:
1. Extract ONLY stock tickers (e.g., AAPL, TSLA, NVDA)
2. EXCLUDE ALL crypto-related tickers: {', '.join(self.crypto_tickers)}
3. EXCLUDE any ticker mentioned with: bitcoin, crypto, cryptocurrency, blockchain, mining
4. Only include if mentioned 3+ times in different articles
5. Only US-listed stocks (no .TO, .L, etc)
6. Focus on actual companies, not indexes

Return ONLY JSON:
{{
  "tickers": ["TICKER1", "TICKER2", ...],
  "reasons": {{"TICKER1": "brief reason why hot", ...}}
}}

Top 5 most mentioned non-crypto stocks only."""

        try:
            response = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.1-70b-versatile",
                temperature=0.2,
                max_tokens=800,
            )
            
            text = response.choices[0].message.content
            text = text.replace('```json', '').replace('```', '').strip()
            result = json.loads(text)
            
            tickers = result.get('tickers', [])
            reasons = result.get('reasons', {})
            
            # 암호화폐 재확인 필터
            filtered = []
            for ticker in tickers:
                if ticker not in self.crypto_tickers:
                    filtered.append(ticker)
                else:
                    print(f"   ❌ {ticker} 제외 (crypto)")
            
            print(f"✅ {len(filtered)}개 핫종목 발견")
            for ticker in filtered:
                reason = reasons.get(ticker, 'Hot this week')
                print(f"   🔥 {ticker}: {reason}")
            
            return filtered, reasons
        
        except Exception as e:
            print(f"❌ AI 분석 오류: {e}")
            return [], {}
    
    def update_portfolio_yaml(self, new_tickers, reasons):
        """portfolio.yaml 업데이트"""
        print("\n📝 portfolio.yaml 업데이트 중...")
        
        try:
            with open('portfolio.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 현재 대체 자산
            current_alts = config.get('alternative_assets', [])
            current_tickers = [a['ticker'] for a in current_alts]
            
            # 새로운 종목 추가
            added = []
            for ticker in new_tickers:
                if ticker not in current_tickers:
                    current_alts.append({
                        'ticker': ticker,
                        'name': reasons.get(ticker, 'Hot stock'),
                        'added': self.now.strftime('%Y-%m-%d'),
                        'auto_added': True
                    })
                    added.append(ticker)
                    print(f"   ➕ {ticker} 추가")
            
            # 3주 이상 뉴스 없는 자동추가 종목 제거
            cutoff_date = (self.now - timedelta(days=21)).strftime('%Y-%m-%d')
            removed = []
            
            filtered_alts = []
            for asset in current_alts:
                # 자동 추가된 것만 제거 대상
                if asset.get('auto_added'):
                    added_date = asset.get('added', '2020-01-01')
                    if added_date < cutoff_date:
                        # 이번 주 뉴스에 있으면 유지
                        if asset['ticker'] not in new_tickers:
                            removed.append(asset['ticker'])
                            print(f"   🗑️ {asset['ticker']} 제거 (3주 경과)")
                            continue
                
                filtered_alts.append(asset)
            
            config['alternative_assets'] = filtered_alts
            
            # 저장
            with open('portfolio.yaml', 'w', encoding='utf-8') as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            
            print(f"✅ 업데이트 완료")
            if added:
                print(f"   추가: {', '.join(added)}")
            if removed:
                print(f"   제거: {', '.join(removed)}")
            if not added and not removed:
                print(f"   변경사항 없음")
            
            return added, removed
        
        except Exception as e:
            print(f"❌ YAML 업데이트 오류: {e}")
            return [], []
    
    def run(self):
        """메인 실행"""
        print("\n" + "="*50)
        print("🤖 Weekly Auto-Update")
        print("="*50)
        
        # 1. 뉴스 수집
        news = self.collect_weekly_news()
        
        if not news:
            print("\n⚠️ 뉴스 없음, 업데이트 스킵")
            return
        
        # 2. 핫종목 추출
        hot_tickers, reasons = self.extract_hot_tickers(news)
        
        if not hot_tickers:
            print("\n⚠️ 핫종목 없음")
            # 그래도 오래된 것 제거는 시도
            self.update_portfolio_yaml([], {})
            return
        
        # 3. YAML 업데이트
        added, removed = self.update_portfolio_yaml(hot_tickers, reasons)
        
        # 4. 요약
        print("\n" + "="*50)
        print("📊 주간 업데이트 완료")
        print("="*50)
        
        if added:
            print(f"\n✅ 추가된 종목 ({len(added)}개):")
            for ticker in added:
                print(f"   🆕 {ticker}")
        
        if removed:
            print(f"\n❌ 제거된 종목 ({len(removed)}개):")
            for ticker in removed:
                print(f"   🗑️ {ticker}")
        
        if not added and not removed:
            print("\n✅ 변경사항 없음")
        
        print("\n✅ 완료!")


if __name__ == "__main__":
    updater = WeeklyUpdater()
    updater.run()
