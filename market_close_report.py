import os
import yaml
import json
import asyncio
import pytz
from datetime import datetime
import yfinance as yf
from telegram import Bot

class MarketCloseReport:
    """장 마감 포트폴리오 정리 리포트"""
    
    def __init__(self):
        print("📊 Market Close Report 초기화...")
        
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        
        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)
        
        self.load_portfolio()
        self.build_ticker_name_map()
        
        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")
    
    def load_portfolio(self):
        """포트폴리오 로드"""
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
                self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
                self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
                self.accumulated_cash = portfolio.get('accumulated_cash', 0)
                print(f"📂 current_portfolio.json 로드")
        except FileNotFoundError:
            print("❌ 포트폴리오 파일 없음")
            self.my_holdings_tfsa1 = {}
            self.my_holdings_tfsa2 = {}
            self.accumulated_cash = 0
    
    def build_ticker_name_map(self):
        """티커 → 이름 매핑"""
        self.ticker_names = {}
        
        try:
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
        except:
            pass
    
    def get_stock_data(self, ticker):
        """주가 데이터 조회"""
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period='2d')
            
            if len(hist) >= 2:
                today_close = hist['Close'].iloc[-1]
                yesterday_close = hist['Close'].iloc[-2]
                daily_return = ((today_close - yesterday_close) / yesterday_close) * 100
                return {
                    'current_price': today_close,
                    'daily_return': daily_return
                }
            elif len(hist) == 1:
                return {
                    'current_price': hist['Close'].iloc[-1],
                    'daily_return': 0
                }
            else:
                return None
        except Exception as e:
            print(f"   ⚠️ {ticker} 데이터 조회 실패: {e}")
            return None
    
    def format_holding_info(self, ticker, holding):
        """보유 자산 정보 포맷"""
        name = self.ticker_names.get(ticker, ticker)
        
        if not isinstance(holding, dict):
            # 기존 포맷 (숫자만)
            return f"{ticker} ({name})\n현재: ${holding:.0f}\n\n", holding, 0, 0
        
        shares = holding.get('shares', 0)
        purchase_price = holding.get('purchase_price', 0)
        purchase_amount = holding.get('amount', 0)
        purchase_date = holding.get('purchase_date', 'N/A')
        
        # 현재 주가 조회
        stock_data = self.get_stock_data(ticker)
        
        if stock_data:
            current_price = stock_data['current_price']
            daily_return = stock_data['daily_return']
            current_value = shares * current_price
            
            # 총 수익률
            if purchase_amount > 0:
                total_return = ((current_value - purchase_amount) / purchase_amount) * 100
            else:
                total_return = 0
            
            # 이모지
            daily_emoji = "🟢" if daily_return >= 0 else "🔴"
            total_emoji = "🟢" if total_return >= 0 else "🔴"
            
            info = f"{ticker} ({name})\n"
            info += f"보유: {shares:.4f}주\n"
            info += f"매수가: ${purchase_price:.2f}\n"
            info += f"현재가: ${current_price:.2f}\n"
            info += f"가치: ${current_value:.2f}\n"
            info += f"오늘: {daily_return:+.2f}% {daily_emoji}\n"
            info += f"총수익: {total_return:+.2f}% {total_emoji}\n"
            
            return info, current_value, daily_return, total_return
        else:
            # 데이터 없으면 기본값
            current_value = purchase_amount
            info = f"{ticker} ({name})\n"
            info += f"가치: ${current_value:.2f}\n"
            info += f"(데이터 조회 불가)\n"
            
            return info, current_value, 0, 0
    
    def generate_report(self):
        """장 마감 리포트 생성"""
        report = f"📊 장 마감 리포트\n"
        report += f"🕐 {self.now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        report += "="*37 + "\n\n"
        
        tfsa1_total = 0
        tfsa1_daily = 0
        
        if self.my_holdings_tfsa1:
            report += "💼 TFSA 1\n\n"
            
            for ticker, holding in self.my_holdings_tfsa1.items():
                info, value, daily, total = self.format_holding_info(ticker, holding)
                report += info + "\n"
                
                tfsa1_total += value
                tfsa1_daily += daily * value / 100  # 달러 금액
            
            report += f"소계: ${tfsa1_total:.2f}\n"
            report += "="*37 + "\n\n"
        
        tfsa2_total = 0
        tfsa2_daily = 0
        
        if self.my_holdings_tfsa2:
            report += "💰 TFSA 2\n\n"
            
            for ticker, holding in self.my_holdings_tfsa2.items():
                info, value, daily, total = self.format_holding_info(ticker, holding)
                report += info + "\n"
                
                tfsa2_total += value
                tfsa2_daily += daily * value / 100
            
            report += f"소계: ${tfsa2_total:.2f}\n"
            report += "="*37 + "\n\n"
        
        # 현금 추가
        if self.accumulated_cash > 0:
            report += f"💵 현금: ${self.accumulated_cash:.2f}\n"
            report += "="*37 + "\n\n"
        
        # 전체 요약
        grand_total = tfsa1_total + tfsa2_total + self.accumulated_cash
        total_daily_change = tfsa1_daily + tfsa2_daily
        
        if grand_total > 0:
            total_daily_pct = (total_daily_change / grand_total) * 100
        else:
            total_daily_pct = 0
        
        daily_emoji = "🟢" if total_daily_change >= 0 else "🔴"
        
        report += "📈 전체 요약\n\n"
        report += f"총 자산: ${grand_total:.2f}\n"
        report += f"오늘 수익: ${total_daily_change:+.2f} ({total_daily_pct:+.2f}%) {daily_emoji}\n"
        
        if not self.my_holdings_tfsa1 and not self.my_holdings_tfsa2:
            report = f"📊 장 마감 리포트\n"
            report += f"🕐 {self.now.strftime('%Y-%m-%d %H:%M %Z')}\n"
            report += "="*37 + "\n\n"
            report += "💵 보유 자산 없음\n"
            if self.accumulated_cash > 0:
                report += f"현금: ${self.accumulated_cash:.2f}\n"
        
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
        print("📊 장 마감 리포트 생성")
        print("="*50)
        
        report = self.generate_report()
        asyncio.run(self.send_telegram(report))
        
        print("\n✅ 완료!")


if __name__ == "__main__":
    reporter = MarketCloseReport()
    reporter.run()
