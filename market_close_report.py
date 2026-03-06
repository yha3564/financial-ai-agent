import os
import yaml
import json
import asyncio
import pytz
import time
import pandas as pd
import yfinance as yf
from datetime import datetime
from telegram import Bot


class MarketCloseReport:
    """장 마감 리포트 v4.0"""

    def __init__(self):
        print("📊 Market Close Report v4.0 초기화...")

        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']

        self.est = pytz.timezone('America/New_York')
        self.now = datetime.now(self.est)

        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        self.load_portfolio()
        self.build_ticker_name_map()

        sold_tickers = [s['ticker'] for s in self.today_sells]
        all_tickers = list(set(
            list(self.my_holdings_tfsa1.keys()) +
            list(self.my_holdings_tfsa2.keys()) +
            sold_tickers
        ))
        self._load_prices(all_tickers)

        print(f"✅ 초기화 완료 - {self.now.strftime('%Y-%m-%d %H:%M %Z')}")

    def load_portfolio(self):
        try:
            with open('current_portfolio.json', 'r', encoding='utf-8') as f:
                portfolio = json.load(f)
            self.my_holdings_tfsa1 = portfolio.get('tfsa1', {})
            self.my_holdings_tfsa2 = portfolio.get('tfsa2', {})
            self.accumulated_cash = portfolio.get('accumulated_cash', 0)
            print(f"📂 포트폴리오 로드 완료")
        except FileNotFoundError:
            print("❌ 포트폴리오 파일 없음")
            self.my_holdings_tfsa1 = {}
            self.my_holdings_tfsa2 = {}
            self.accumulated_cash = 0

        for asset in self.config.get('tfsa2_assets', []):
            ticker = asset['ticker']
            if ticker in self.my_holdings_tfsa2:
                self.my_holdings_tfsa2[ticker]['purpose'] = asset.get('purpose', '')
                self.my_holdings_tfsa2[ticker]['target_amount'] = asset.get('target_amount', 0)

        self.today_sells = []
        try:
            with open('today_sold.json', 'r', encoding='utf-8') as f:
                sold_data = json.load(f)
            if sold_data.get('date') == self.now.strftime('%Y-%m-%d'):
                self.today_sells = sold_data.get('sells', [])
            print(f"📋 오늘 매도 기록: {len(self.today_sells)}건")
        except FileNotFoundError:
            print("📋 오늘 매도 기록 없음")

    def build_ticker_name_map(self):
        self.ticker_names = {}
        for section in ['tfsa1_assets', 'tfsa2_assets', 'alternative_assets', 'safe_assets']:
            for asset in self.config.get(section, []):
                self.ticker_names[asset['ticker']] = asset.get('name', asset['ticker'])

    def _load_prices(self, tickers):
        if not tickers:
            self._prices = {}
            self._hist = {}
            return

        print(f"📥 가격 로드 ({len(tickers)}개)...")
        self._prices = {}
        self._hist = {}

        try:
            tickers_str = " ".join(tickers)
            df = yf.download(tickers_str, period="5d", auto_adjust=True,
                           progress=False, threads=False)

            if df.empty:
                raise ValueError("빈 데이터")

            if isinstance(df.columns, pd.MultiIndex):
                for ticker in tickers:
                    try:
                        close = df['Close'][ticker].dropna()
                        if len(close) > 0:
                            self._prices[ticker] = float(close.iloc[-1])
                            self._hist[ticker] = pd.DataFrame({'Close': close})
                        else:
                            self._prices[ticker] = 0
                    except:
                        self._prices[ticker] = 0
            else:
                ticker = tickers[0]
                close = df['Close'].dropna()
                if len(close) > 0:
                    self._prices[ticker] = float(close.iloc[-1])
                    self._hist[ticker] = pd.DataFrame({'Close': close})

        except Exception as e:
            print(f"⚠️ 배치 로드 오류: {e} - 개별 재시도...")
            for ticker in tickers:
                try:
                    df = yf.download(ticker, period="5d", auto_adjust=True,
                                   progress=False, threads=False)
                    if not df.empty and 'Close' in df.columns:
                        close = df['Close'].dropna()
                        if len(close) > 0:
                            self._prices[ticker] = float(close.iloc[-1])
                            self._hist[ticker] = pd.DataFrame({'Close': close})
                            continue
                except:
                    pass
                self._prices[ticker] = 0

    def get_price(self, ticker):
        return self._prices.get(ticker, 0)

    def get_daily_return(self, ticker):
        hist = self._hist.get(ticker)
        if hist is None or len(hist) < 2:
            return 0
        try:
            close = hist['Close'].dropna()
            today = float(close.iloc[-1])
            yesterday = float(close.iloc[-2])
            return (today - yesterday) / yesterday * 100
        except:
            return 0

    def generate_report(self):
        report = f"📊 장마감 브리핑\n🕐 {self.now.strftime('%Y-%m-%d %H:%M EST')}\n"
        report += "=" * 37 + "\n"

        tfsa1_total = 0
        tfsa1_daily_dollar = 0

        # 오늘 매도 실현손익
        if self.today_sells:
            report += "📤 오늘 매도 실현손익\n"
            report += "=" * 37 + "\n"
            total_realized = 0

            has_slippage = any(s.get('recommended_price') for s in self.today_sells)
            total_slippage = 0

            for s in self.today_sells:
                ticker = s['ticker']
                name = self.ticker_names.get(ticker, ticker)
                shares = s['shares']
                avg_price = s['avg_price']
                sell_price = s['sell_price']
                sell_value = s['sell_value']
                profit = s['profit']
                profit_pct = s['profit_pct']
                type_label = "전량" if s['type'] == 'full' else "절반" if s['type'] == 'half' else "부분"
                profit_emoji = "🟢" if profit >= 0 else "🔴"
                total_realized += profit

                report += f"{ticker} ({name}) {type_label}매도\n"
                report += f"{shares}주 @${sell_price:.2f} = ${sell_value:.2f}\n"
                report += f"평균단가 ${avg_price:.2f} → 매도가 ${sell_price:.2f}\n"
                report += f"실현손익: {profit:+.2f}$ ({profit_pct:+.2f}%) {profit_emoji}\n"

                recommended_price = s.get('recommended_price')
                if recommended_price and recommended_price > 0:
                    slippage = sell_price - recommended_price
                    slippage_dollar = slippage * shares
                    total_slippage += slippage_dollar
                    slip_emoji = "🟢" if slippage >= 0 else "🔴"
                    report += f"슬리피지: 추천가 ${recommended_price:.2f} → 체결가 ${sell_price:.2f} ({slippage:+.2f}$/주 × {shares}주 = {slippage_dollar:+.2f}$) {slip_emoji}\n"

                report += "\n"

            total_emoji = "🟢" if total_realized >= 0 else "🔴"
            report += f"실현손익 합계: {total_realized:+.2f}$ {total_emoji}\n"

            if has_slippage:
                slip_emoji = "🟢" if total_slippage >= 0 else "🔴"
                report += f"슬리피지 합계: {total_slippage:+.2f}$ {slip_emoji}\n"

            report += "=" * 37 + "\n"

        # TFSA 1
        if self.my_holdings_tfsa1:
            report += f"💼 TFSA 1 | 💵 현금: ${self.accumulated_cash:.0f} CAD\n"
            for ticker, holding in self.my_holdings_tfsa1.items():
                shares = holding.get('shares', 0)
                avg_price = holding.get('avg_price', 0)
                price = self.get_price(ticker)
                daily_pct = self.get_daily_return(ticker)

                if price <= 0:
                    continue

                value = shares * price
                profit_pct = (price - avg_price) / avg_price * 100 if avg_price > 0 else 0
                daily_dollar = value * daily_pct / 100

                tfsa1_total += value
                tfsa1_daily_dollar += daily_dollar

                daily_emoji = "🟢" if daily_pct >= 0 else "🔴"
                profit_emoji = "🟢" if profit_pct >= 0 else "🔴"

                name = self.ticker_names.get(ticker, ticker)

                report += f"{ticker} ({name})\n"
                report += f"{shares}주 × ${price:.2f} = ${value:.2f}\n"
                report += f"오늘: {daily_pct:+.2f}% ({daily_dollar:+.2f}$) {daily_emoji}\n"
                report += f"수익: {profit_pct:+.2f}% {profit_emoji}\n\n"

            tfsa1_daily_pct = tfsa1_daily_dollar / tfsa1_total * 100 if tfsa1_total > 0 else 0
            daily_emoji = "🟢" if tfsa1_daily_dollar >= 0 else "🔴"

            report += f"소계: ${tfsa1_total:.2f}  오늘 {tfsa1_daily_pct:+.2f}% {daily_emoji}\n"
            report += "=" * 37 + "\n"

        # TFSA 2
        tfsa2_total = 0
        tfsa2_daily_dollar = 0

        if self.my_holdings_tfsa2:
            report += "💰 TFSA 2 | 💵 현금: $0\n"

            for ticker, holding in self.my_holdings_tfsa2.items():
                shares = holding.get('shares', 0)
                avg_price = holding.get('avg_price', 0)
                purpose = holding.get('purpose', '')
                target = holding.get('target_amount', 0)
                price = self.get_price(ticker)
                daily_pct = self.get_daily_return(ticker)

                if price <= 0:
                    continue

                value = shares * price
                profit_pct = (price - avg_price) / avg_price * 100 if avg_price > 0 else 0
                daily_dollar = value * daily_pct / 100

                tfsa2_total += value
                tfsa2_daily_dollar += daily_dollar

                name = self.ticker_names.get(ticker, ticker)
                purpose_label = "여자친구 자금" if "girlfriend" in purpose else "어머님 자금" if "mother" in purpose else ""

                daily_emoji = "🟢" if daily_pct >= 0 else "🔴"
                profit_emoji = "🟢" if profit_pct >= 0 else "🔴"

                report += f"{ticker} ({name}) | {purpose_label}\n"
                report += f"{shares}주 × ${price:.2f} = ${value:.2f}\n"
                report += f"오늘: {daily_pct:+.2f}% ({daily_dollar:+.2f}$) {daily_emoji}\n"
                report += f"수익: {profit_pct:+.2f}% {profit_emoji}\n"

                if target > 0:
                    progress = value / target * 100
                    report += f"목표: ${value:.0f} / ${target:.0f} ({progress:.1f}%)\n"
                report += "\n"

            tfsa2_daily_pct = tfsa2_daily_dollar / tfsa2_total * 100 if tfsa2_total > 0 else 0
            daily_emoji = "🟢" if tfsa2_daily_dollar >= 0 else "🔴"
            report += f"소계: ${tfsa2_total:.2f}  오늘 {tfsa2_daily_pct:+.2f}% {daily_emoji}\n"
            report += "=" * 37 + "\n"

        # 전체 요약
        grand_total = tfsa1_total + tfsa2_total
        total_daily = tfsa1_daily_dollar + tfsa2_daily_dollar
        total_daily_pct = total_daily / grand_total * 100 if grand_total > 0 else 0
        daily_emoji = "🟢" if total_daily >= 0 else "🔴"

        report += f"📈 전체 요약\n"
        report += f"총 자산: ${grand_total:.2f}\n"
        report += f"오늘 수익: {total_daily:+.2f}$ ({total_daily_pct:+.2f}%) {daily_emoji}\n"
        report += "📌 장후 뉴스 수집 시작\n"

        return report

    async def send_telegram(self, message):
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
        try:
            print("\n" + "=" * 50)
            print("📊 장 마감 리포트 생성")
            print("=" * 50)

            report = self.generate_report()
            asyncio.run(self.send_telegram(report))
            print("\n✅ 완료!")

        except Exception as e:
            error_msg = f"⚠️ Market Close Report 오류\n🕐 {self.now.strftime('%H:%M EST')}\n❌ {str(e)}"
            try:
                asyncio.run(self.send_telegram(error_msg))
            except:
                pass
            raise


def is_market_open():
    est = pytz.timezone('America/New_York')
    now = datetime.now(est)
    if now.weekday() >= 5:
        print(f"📅 주말 ({now.strftime('%A')}) — 스킵")
        return False
    us_holidays = [
        '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03',
        '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
        '2026-11-26', '2026-12-25',
    ]
    if now.strftime('%Y-%m-%d') in us_holidays:
        print(f"📅 휴장일 — 스킵")
        return False
    return True


if __name__ == "__main__":
    if not is_market_open():
        print("🛑 장 휴무 — 스킵")
    else:
        reporter = MarketCloseReport()
        reporter.run()
