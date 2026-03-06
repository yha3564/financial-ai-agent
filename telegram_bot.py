import os
import json
import hmac
import hashlib
import asyncio
import base64
import requests
from datetime import datetime
import pytz
from flask import Flask, request, jsonify, render_template_string
from telegram import Bot

# Gemini Vision (스크린샷 인식)
try:
    from google import genai as genai_new
    USE_NEW_GENAI = True
except ImportError:
    try:
        import google.generativeai as genai
        USE_NEW_GENAI = False
    except:
        USE_NEW_GENAI = None

# ============================================================
# Flask 앱 (미니앱 서빙 + 웹훅)
# ============================================================
flask_app = Flask(__name__)

TELEGRAM_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
GH_TOKEN = os.environ['GH_TOKEN']
GITHUB_REPO = os.environ.get('GITHUB_REPOSITORY', 'yha3564/financial-ai-agent')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
RENDER_URL = (
    os.environ.get('RENDER_EXTERNAL_URL') or
    os.environ.get('RENDER_URL') or
    ''
).rstrip('/')

est = pytz.timezone('America/New_York')

# 유저 상태 (메모리, 재시작 시 초기화)
user_state = {}

def read_github_file(filename):
    """GitHub repo에서 JSON 파일 읽기"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github.v3.raw'}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return json.loads(resp.text)
    except:
        pass
    return None

def write_github_file(filename, data, message="Update"):
    """GitHub repo에 JSON 파일 쓰기"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {'Authorization': f'token {GH_TOKEN}'}
    
    # 기존 파일 SHA 가져오기
    resp = requests.get(url, headers=headers, timeout=10)
    sha = resp.json().get('sha', '') if resp.status_code == 200 else ''
    
    content = base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode()).decode()
    
    payload = {'message': message, 'content': content}
    if sha:
        payload['sha'] = sha
    
    requests.put(url, headers=headers, json=payload, timeout=10)


# ============================================================
# Gemini Vision (스크린샷 인식)
# ============================================================
def analyze_screenshot(image_base64):
    """Gemini Vision으로 체결/배당 스크린샷 분석"""
    if not GEMINI_API_KEY:
        return None
    try:
        prompt = """이 Wealthsimple 스크린샷에서 다음 정보를 추출해주세요.
JSON으로만 응답하세요 (다른 텍스트 없이):
{
  "type": "trade" 또는 "dividend",
  "ticker": "종목코드",
  "shares": 주수(숫자),
  "price": 가격(숫자),
  "amount": 총액(숫자),
  "action": "buy" 또는 "sell" 또는 "dividend"
}
인식 불가 시: {"type": "unknown"}"""

        if USE_NEW_GENAI:
            client = genai_new.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[
                    {'inline_data': {'mime_type': 'image/jpeg', 'data': image_base64}},
                    prompt
                ]
            )
        elif USE_NEW_GENAI is False:
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.0-flash')
            response = model.generate_content([
                {'inline_data': {'mime_type': 'image/jpeg', 'data': image_base64}},
                prompt
            ])
        else:
            return None

        text = response.text.strip()
        # JSON 추출
        if '```' in text:
            text = text.split('```')[1].replace('json', '').strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ 스크린샷 분석 오류: {e}")
        return None


def send_telegram_msg(chat_id, text, keyboard=None):
    """텔레그램 메시지 전송 헬퍼"""
    bot_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    payload = {'chat_id': chat_id, 'text': text}
    if keyboard:
        payload['reply_markup'] = json.dumps(keyboard)
    requests.post(f"{bot_url}/sendMessage", json=payload)


def get_investment_recommendation(cash_amount, dividend_ticker=None):
    """현금으로 투자 추천 생성"""
    portfolio = read_github_file('current_portfolio.json')

    # TFSA2 배당금 → 본체 자산 추가매수
    if dividend_ticker and portfolio:
        tfsa2 = portfolio.get('tfsa2', {})
        for t, data in tfsa2.items():
            origin = data.get('origin_ticker', '')
            # 배당 티커가 본체이거나 현재 보유 중인 TFSA2 자산이면
            if dividend_ticker == origin or dividend_ticker == t:
                import yfinance as yf
                try:
                    price = float(yf.download(t, period='1d', progress=False)['Close'].iloc[-1])
                except:
                    price = data.get('avg_price', 0)
                if price > 0:
                    shares = round(cash_amount / price, 4)
                    return (f"📊 TFSA2 추가매수 추천:\n"
                            f"{t}\n{shares}주 @${price:.2f} = ${cash_amount:.2f}",
                            'tfsa2', t, shares, price)

    # TFSA1 추천
    pending = read_github_file('pending_trades.json')
    if not pending:
        return (f"💵 현금 ${cash_amount:.2f} 적립\n📊 다음 브리핑에서 추천이 나옵니다.",
                None, None, 0, 0)

    tfsa1 = pending.get('tfsa1', [])
    buys = [a for a in tfsa1 if a.get('action') == 'BUY']
    if buys:
        best = max(buys, key=lambda x: x.get('score', 0))
        ticker = best['ticker']
        price = best['price']
        shares = round(cash_amount / price, 4) if price > 0 else 0
        pct = best.get('expected_pct', 0)
        return (f"📊 투자 추천:\n"
                f"{ticker}\n{shares}주 @${price:.2f} = ${cash_amount:.2f}  ({pct:+.1f}% 예상)",
                'tfsa1', ticker, shares, price)

    return (f"💵 현금 ${cash_amount:.2f} 적립\n📊 현재 매수 추천 없음, 다음 브리핑까지 보유",
            None, None, 0, 0)


def save_watch_state(recommendations):
    """관망 상태 저장 (다음날 재알림용)"""
    watch_data = {
        'date': datetime.now(est).strftime('%Y-%m-%d'),
        'recommendations': recommendations
    }
    write_github_file('watch_state.json', watch_data, '👀 관망 상태 저장')

# ============================================================
# 미니앱 HTML
# ============================================================
MINIAPP_HTML = '''<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>체결가 입력</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: var(--tg-theme-bg-color, #1c1c1e);
      color: var(--tg-theme-text-color, #ffffff);
      padding: 16px;
      min-height: 100vh;
    }
    h2 {
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 4px;
    }
    .subtitle {
      font-size: 13px;
      color: var(--tg-theme-hint-color, #8e8e93);
      margin-bottom: 20px;
    }
    .section {
      margin-bottom: 24px;
    }
    .section-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--tg-theme-hint-color, #8e8e93);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 8px;
      padding-bottom: 4px;
      border-bottom: 1px solid rgba(255,255,255,0.1);
    }
    .trade-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 0;
      border-bottom: 1px solid rgba(255,255,255,0.05);
    }
    .trade-row:last-child { border-bottom: none; }
    .trade-info { flex: 1; }
    .trade-ticker {
      font-size: 15px;
      font-weight: 600;
    }
    .trade-detail {
      font-size: 12px;
      color: var(--tg-theme-hint-color, #8e8e93);
      margin-top: 2px;
    }
    .trade-badge {
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 10px;
      margin-right: 10px;
    }
    .badge-buy { background: rgba(52,199,89,0.2); color: #34c759; }
    .badge-sell-auto {
      background: rgba(142,142,147,0.2);
      color: #8e8e93;
      font-size: 10px;
    }
    .price-input {
      width: 110px;
      padding: 8px 10px;
      background: var(--tg-theme-secondary-bg-color, #2c2c2e);
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 8px;
      color: var(--tg-theme-text-color, #ffffff);
      font-size: 15px;
      text-align: right;
    }
    .price-input:focus {
      outline: none;
      border-color: #0a84ff;
    }
    .price-input::placeholder { color: #8e8e93; }
    .auto-label {
      font-size: 12px;
      color: #8e8e93;
      font-style: italic;
      width: 110px;
      text-align: right;
    }
    .submit-btn {
      width: 100%;
      padding: 14px;
      background: #0a84ff;
      color: white;
      border: none;
      border-radius: 12px;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      margin-top: 8px;
    }
    .submit-btn:active { opacity: 0.8; }
    .submit-btn:disabled { background: #8e8e93; cursor: not-allowed; }
    .error-msg {
      color: #ff453a;
      font-size: 13px;
      margin-top: 8px;
      display: none;
    }
    .success-msg {
      text-align: center;
      padding: 40px 20px;
      display: none;
    }
    .success-icon { font-size: 48px; margin-bottom: 12px; }
    .success-text { font-size: 18px; font-weight: 600; }
  </style>
</head>
<body>
  <div id="main-content">
    <h2>✅ 체결가 입력</h2>
    <p class="subtitle" id="timestamp"></p>
    <div id="trades-container"></div>
    <p class="error-msg" id="error-msg">모든 매도/매수 가격과 주수를 입력해주세요.</p>
    <button class="submit-btn" id="submit-btn" onclick="submitTrades()">기록 완료</button>
  </div>

  <div class="success-msg" id="success-msg">
    <div class="success-icon">✅</div>
    <div class="success-text">기록 완료!</div>
    <p style="margin-top:8px;color:#8e8e93;font-size:14px">포트폴리오가 업데이트됐어요</p>
  </div>

  <script>
    const tg = window.Telegram.WebApp;
    tg.ready();
    tg.expand();

    let pendingTrades = null;

    // pending_trades 로드
    async function loadTrades() {
      try {
        const res = await fetch('/api/pending_trades');
        pendingTrades = await res.json();
        renderTrades(pendingTrades);

        const ts = new Date(pendingTrades.timestamp);
        document.getElementById('timestamp').textContent =
          ts.toLocaleTimeString('ko-KR', {hour: '2-digit', minute: '2-digit'}) + ' 추천';
      } catch(e) {
        document.getElementById('trades-container').innerHTML =
          '<p style="color:#ff453a">거래 정보를 불러올 수 없어요</p>';
      }
    }

    function renderTrades(data) {
      const container = document.getElementById('trades-container');
      let html = '';

      // TFSA 1
      const tfsa1_buys = (data.tfsa1 || []).filter(a => a.action === 'BUY');
      const tfsa1_sells = (data.tfsa1 || []).filter(a => a.action === 'SELL');

      if (tfsa1_buys.length > 0 || tfsa1_sells.length > 0) {
        html += '<div class="section">';
        html += '<div class="section-title">TFSA 1</div>';

        // 매도 (체결가 입력)
        tfsa1_sells.forEach(s => {
          const typeLabel = s.type === 'full' ? '전량매도' : s.type === 'half' ? '절반매도' : '부분매도';
          html += `<div class="trade-row">
            <div class="trade-info">
              <div class="trade-ticker">${s.ticker}</div>
              <div class="trade-detail">${s.shares}주 ${typeLabel}</div>
            </div>
            <input class="price-input" type="number" step="0.01"
              id="sell_price_tfsa1_${s.ticker}"
              placeholder="$0.00"
              inputmode="decimal">
            <span class="trade-badge badge-sell-auto">${typeLabel}</span>
          </div>`;
        });

        // 매수 (주수 + 가격 입력)
        tfsa1_buys.forEach(b => {
          html += `<div class="trade-row">
            <div class="trade-info">
              <div class="trade-ticker">${b.ticker}</div>
              <div class="trade-detail">매수</div>
            </div>
            <span class="trade-badge badge-buy">매수</span>
            <input class="price-input" type="number" step="0.0001"
              id="shares_tfsa1_${b.ticker}"
              placeholder="주수"
              inputmode="decimal"
              style="width:80px;margin-right:4px">
            <input class="price-input" type="number" step="0.01"
              id="price_tfsa1_${b.ticker}"
              placeholder="$0.00"
              inputmode="decimal">
          </div>`;
        });

        html += '</div>';
      }

      // TFSA 2
      const tfsa2 = data.tfsa2 || {};
      Object.entries(tfsa2).forEach(([ticker, data]) => {
        const purpose = data.purpose || '';
        const label = purpose.includes('girlfriend') ? '여자친구 자금' :
                      purpose.includes('mother') ? '어머님 자금' : ticker;
        const sells = (data.actions || []).filter(a => a.action === 'SELL');
        const buys = (data.actions || []).filter(a => a.action === 'BUY');

        if (sells.length === 0 && buys.length === 0) return;

        html += `<div class="section">`;
        html += `<div class="section-title">TFSA 2 | ${label}</div>`;

        sells.forEach(s => {
          html += `<div class="trade-row">
            <div class="trade-info">
              <div class="trade-ticker">${s.ticker}</div>
              <div class="trade-detail">${s.shares}주 전량매도</div>
            </div>
            <input class="price-input" type="number" step="0.01"
              id="sell_price_tfsa2_${ticker}_${s.ticker}"
              placeholder="$0.00"
              inputmode="decimal">
            <span class="trade-badge badge-sell-auto">전량매도</span>
          </div>`;
        });

        buys.forEach(b => {
          html += `<div class="trade-row">
            <div class="trade-info">
              <div class="trade-ticker">${b.ticker}</div>
              <div class="trade-detail">매수</div>
            </div>
            <span class="trade-badge badge-buy">매수</span>
            <input class="price-input" type="number" step="0.0001"
              id="shares_tfsa2_${ticker}_${b.ticker}"
              placeholder="주수"
              inputmode="decimal"
              style="width:80px;margin-right:4px">
            <input class="price-input" type="number" step="0.01"
              id="price_tfsa2_${ticker}_${b.ticker}"
              placeholder="$0.00"
              inputmode="decimal">
          </div>`;
        });

        html += '</div>';
      });

      container.innerHTML = html || '<p style="color:#8e8e93">입력할 거래가 없어요</p>';
    }

    async function submitTrades() {
      if (!pendingTrades) return;

      // 매도 체결가 + 매수 주수/가격 수집
      const prices = {};
      const shares = {};
      let allFilled = true;

      // TFSA1 매도 체결가
      const tfsa1_sells = (pendingTrades.tfsa1 || []).filter(a => a.action === 'SELL');
      tfsa1_sells.forEach(s => {
        const val = document.getElementById(`sell_price_tfsa1_${s.ticker}`)?.value;
        if (!val || parseFloat(val) <= 0) { allFilled = false; return; }
        prices[`sell_tfsa1_${s.ticker}`] = parseFloat(val);
      });

      // TFSA1 매수 주수+가격
      const tfsa1_buys = (pendingTrades.tfsa1 || []).filter(a => a.action === 'BUY');
      tfsa1_buys.forEach(b => {
        const priceVal = document.getElementById(`price_tfsa1_${b.ticker}`)?.value;
        const sharesVal = document.getElementById(`shares_tfsa1_${b.ticker}`)?.value;
        if (!priceVal || parseFloat(priceVal) <= 0) { allFilled = false; return; }
        if (!sharesVal || parseFloat(sharesVal) <= 0) { allFilled = false; return; }
        prices[`tfsa1_${b.ticker}`] = parseFloat(priceVal);
        shares[`tfsa1_${b.ticker}`] = parseFloat(sharesVal);
      });

      const tfsa2 = pendingTrades.tfsa2 || {};
      Object.entries(tfsa2).forEach(([ticker, data]) => {
        // TFSA2 매도 체결가
        const sells = (data.actions || []).filter(a => a.action === 'SELL');
        sells.forEach(s => {
          const val = document.getElementById(`sell_price_tfsa2_${ticker}_${s.ticker}`)?.value;
          if (!val || parseFloat(val) <= 0) { allFilled = false; return; }
          prices[`sell_tfsa2_${ticker}_${s.ticker}`] = parseFloat(val);
        });

        // TFSA2 매수 주수+가격
        const buys = (data.actions || []).filter(a => a.action === 'BUY');
        buys.forEach(b => {
          const priceVal = document.getElementById(`price_tfsa2_${ticker}_${b.ticker}`)?.value;
          const sharesVal = document.getElementById(`shares_tfsa2_${ticker}_${b.ticker}`)?.value;
          if (!priceVal || parseFloat(priceVal) <= 0) { allFilled = false; return; }
          if (!sharesVal || parseFloat(sharesVal) <= 0) { allFilled = false; return; }
          prices[`tfsa2_${ticker}_${b.ticker}`] = parseFloat(priceVal);
          shares[`tfsa2_${ticker}_${b.ticker}`] = parseFloat(sharesVal);
        });
      });

      if (!allFilled) {
        document.getElementById('error-msg').style.display = 'block';
        return;
      }

      document.getElementById('error-msg').style.display = 'none';
      document.getElementById('submit-btn').disabled = true;
      document.getElementById('submit-btn').textContent = '저장 중...';

      try {
        const res = await fetch('/api/submit_trades', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ trades: pendingTrades, prices, shares })
        });
        const result = await res.json();

        if (result.success) {
          document.getElementById('main-content').style.display = 'none';
          document.getElementById('success-msg').style.display = 'block';
          setTimeout(() => tg.close(), 2000);
        } else {
          document.getElementById('submit-btn').disabled = false;
          document.getElementById('submit-btn').textContent = '기록 완료';
          alert('저장 실패: ' + result.error);
        }
      } catch(e) {
        document.getElementById('submit-btn').disabled = false;
        document.getElementById('submit-btn').textContent = '기록 완료';
        alert('오류가 발생했어요');
      }
    }

    loadTrades();
  </script>
</body>
</html>'''


# ============================================================
# Flask 라우트
# ============================================================

@flask_app.route('/')
def index():
    return 'Financial AI Agent Bot Running ✅'


@flask_app.route('/miniapp')
def miniapp():
    return render_template_string(MINIAPP_HTML)


@flask_app.route('/api/pending_trades')
def get_pending_trades():
    data = read_github_file('pending_trades.json')
    if data:
        return jsonify(data)
    return jsonify({}), 404


@flask_app.route('/api/submit_trades', methods=['POST'])
def submit_trades():
    try:
        data = request.get_json()
        trades = data.get('trades', {})
        prices = data.get('prices', {})
        actual_shares = data.get('shares', {})

        # 포트폴리오 로드
        portfolio = read_github_file('current_portfolio.json')
        if not portfolio:
            return jsonify({'success': False, 'error': 'portfolio not found'}), 404

        now = datetime.now(est)

        # ── TFSA 1 처리 ──
        tfsa1_actions = trades.get('tfsa1', [])
        sells = [a for a in tfsa1_actions if a['action'] == 'SELL']
        buys = [a for a in tfsa1_actions if a['action'] == 'BUY']

        # 매도 처리 (체결가 기준)
        sell_total = 0
        sold_history = []
        for sell in sells:
            ticker = sell['ticker']
            sell_price = prices.get(f'sell_tfsa1_{ticker}', 0)
            sell_shares = sell['shares']
            sell_total += sell_price * sell_shares

            existing = portfolio.get('tfsa1', {}).get(ticker, {})
            old_shares = existing.get('shares', 0)
            avg_price = existing.get('avg_price', 0)
            remaining = round(old_shares - sell_shares, 4)

            # 실현 손익 기록
            sold_history.append({
                'ticker': ticker,
                'shares': sell_shares,
                'avg_price': avg_price,
                'sell_price': sell_price,
                'sell_value': round(sell_price * sell_shares, 2),
                'profit': round((sell_price - avg_price) * sell_shares, 2),
                'profit_pct': round((sell_price - avg_price) / avg_price * 100, 2) if avg_price > 0 else 0,
                'type': sell.get('type', 'full'),
                'account': 'TFSA1'
            })

            if remaining <= 0 or sell['type'] == 'full':
                if ticker in portfolio.get('tfsa1', {}):
                    del portfolio['tfsa1'][ticker]
            else:
                portfolio['tfsa1'][ticker]['shares'] = remaining

        # 매수 처리 (실제 입력 주수+가격 기준)
        buy_total = 0
        for buy in buys:
            ticker = buy['ticker']
            buy_price = prices.get(f'tfsa1_{ticker}', 0)
            buy_shares = actual_shares.get(f'tfsa1_{ticker}', buy['shares'])
            if buy_price <= 0:
                continue

            buy_total += buy_price * buy_shares
            existing = portfolio['tfsa1'].get(ticker, {})
            old_shares = existing.get('shares', 0)
            old_avg = existing.get('avg_price', 0)

            if old_shares > 0 and old_avg > 0:
                new_avg = (old_shares * old_avg + buy_shares * buy_price) / (old_shares + buy_shares)
            else:
                new_avg = buy_price

            portfolio['tfsa1'][ticker] = {
                'shares': round(old_shares + buy_shares, 4),
                'avg_price': round(new_avg, 4)
            }

        # accumulated_cash: 기존현금 + 매도금 - 매수금
        portfolio['accumulated_cash'] = max(0, portfolio.get('accumulated_cash', 0) + sell_total - buy_total)

        # ── TFSA 2 처리 ──
        tfsa2_actions = trades.get('tfsa2', {})
        for holder_ticker, data in tfsa2_actions.items():
            actions = data.get('actions', [])
            sell_actions = [a for a in actions if a['action'] == 'SELL']
            buy_actions = [a for a in actions if a['action'] == 'BUY']
            purpose_info = {}

            for sell in sell_actions:
                ticker = sell['ticker']
                existing = portfolio.get('tfsa2', {}).get(ticker, {})
                avg_price = existing.get('avg_price', 0)
                sell_price = prices.get(f'sell_tfsa2_{holder_ticker}_{ticker}', 0)
                sell_shares = sell['shares']

                sold_history.append({
                    'ticker': ticker,
                    'shares': sell_shares,
                    'avg_price': avg_price,
                    'sell_price': sell_price,
                    'sell_value': round(sell_price * sell_shares, 2),
                    'profit': round((sell_price - avg_price) * sell_shares, 2),
                    'profit_pct': round((sell_price - avg_price) / avg_price * 100, 2) if avg_price > 0 else 0,
                    'type': sell.get('type', 'full'),
                    'account': 'TFSA2'
                })

                if ticker in portfolio.get('tfsa2', {}):
                    purpose_info = {
                        k: v for k, v in portfolio['tfsa2'][ticker].items()
                        if k in ['purpose', 'target_amount']
                    }
                    del portfolio['tfsa2'][ticker]

            for buy in buy_actions:
                ticker = buy['ticker']
                buy_price = prices.get(f'tfsa2_{holder_ticker}_{ticker}', 0)
                buy_shares = actual_shares.get(f'tfsa2_{holder_ticker}_{ticker}', buy['shares'])
                if buy_price <= 0:
                    continue

                existing = portfolio['tfsa2'].get(ticker, {})
                old_shares = existing.get('shares', 0)
                old_avg = existing.get('avg_price', 0)

                if old_shares > 0 and old_avg > 0:
                    new_avg = (old_shares * old_avg + buy_shares * buy_price) / (old_shares + buy_shares)
                else:
                    new_avg = buy_price

                portfolio['tfsa2'][ticker] = {
                    'shares': round(old_shares + buy_shares, 4),
                    'avg_price': round(new_avg, 4),
                    'purpose': purpose_info.get('purpose', data.get('purpose', '')),
                    'target_amount': purpose_info.get('target_amount', 0),
                    'origin_ticker': holder_ticker if holder_ticker != ticker else ''
                }

        # 시간 업데이트
        portfolio['date'] = now.strftime('%Y-%m-%d')
        portfolio['time'] = now.strftime('%H:%M')

        # GitHub에 저장
        write_github_file('current_portfolio.json', portfolio, '💼 포트폴리오 업데이트')

        # 오늘 매도 실현손익 저장
        if sold_history:
            existing_sold = read_github_file('today_sold.json') or {}
            if existing_sold.get('date') != now.strftime('%Y-%m-%d'):
                existing_sold = {'date': now.strftime('%Y-%m-%d'), 'sells': []}
            existing_sold['sells'].extend(sold_history)
            write_github_file('today_sold.json', existing_sold, '📈 매도 기록 업데이트')

        return jsonify({'success': True, 'pushed': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================
# 웹훅 라우트
# ============================================================

@flask_app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return 'OK'

    bot_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    now_str = datetime.now(est).strftime('%H:%M EST')

    # ── 일반 메시지 처리 (텍스트/사진) ──
    message = data.get('message')
    if message:
        chat_id = message.get('chat', {}).get('id')
        if not chat_id:
            return 'OK'

        # 사진 처리 (스크린샷 인식)
        if message.get('photo'):
            photo = message['photo'][-1]  # 최고 해상도
            file_id = photo['file_id']
            try:
                # 파일 다운로드
                file_resp = requests.get(f"{bot_url}/getFile", params={'file_id': file_id}).json()
                file_path = file_resp['result']['file_path']
                file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                img_data = requests.get(file_url).content
                img_b64 = base64.b64encode(img_data).decode()

                result = analyze_screenshot(img_b64)
                if result and result.get('type') != 'unknown':
                    ticker = result.get('ticker', '?')
                    action = result.get('action', '?')
                    shares = result.get('shares', 0)
                    price = result.get('price', 0)
                    amount = result.get('amount', 0)

                    if result['type'] == 'dividend':
                        text = f"💰 배당금 인식:\n{ticker} ${amount:.2f}\n\n맞으면 ✅, 틀리면 ❌"
                        user_state[str(chat_id)] = {'mode': 'confirm_dividend', 'amount': amount, 'ticker': ticker}
                    else:
                        action_kr = '매수' if action == 'buy' else '매도'
                        text = f"📝 체결 인식:\n{ticker} {action_kr} {shares}주 @${price:.2f}\n\n맞으면 ✅, 틀리면 ❌"
                        user_state[str(chat_id)] = {
                            'mode': 'confirm_trade',
                            'ticker': ticker, 'action': action,
                            'shares': shares, 'price': price
                        }

                    keyboard = {'inline_keyboard': [[
                        {'text': '✅ 맞아', 'callback_data': 'screenshot_confirm'},
                        {'text': '❌ 다시', 'callback_data': 'screenshot_reject'}
                    ]]}
                    send_telegram_msg(chat_id, text, keyboard)
                else:
                    send_telegram_msg(chat_id, "⚠️ 인식 실패. 수동으로 입력해주세요.\n예: 55.20")
            except Exception as e:
                print(f"⚠️ 사진 처리 오류: {e}")
                send_telegram_msg(chat_id, "⚠️ 사진 처리 실패. 금액을 직접 입력해주세요.")
            return 'OK'

        # 텍스트 메시지 처리
        text = message.get('text', '').strip()
        state = user_state.get(str(chat_id), {})

        if state.get('mode') == 'waiting_dividend_amount':
            try:
                amount = float(text.replace('$', '').replace(',', ''))
                portfolio = read_github_file('current_portfolio.json')
                if portfolio:
                    old_cash = portfolio.get('accumulated_cash', 0)
                    portfolio['accumulated_cash'] = old_cash + amount
                    write_github_file('current_portfolio.json', portfolio, '💰 배당금 입금')

                    div_ticker = state.get('dividend_ticker')
                    div_account = state.get('dividend_account', 'tfsa1')
                    rec_result = get_investment_recommendation(portfolio['accumulated_cash'], div_ticker)
                    rec_text, rec_account, rec_ticker, rec_shares, rec_price = rec_result

                    account_label = 'TFSA 2' if div_account == 'tfsa2' else 'TFSA 1'
                    msg = (f"💰 {account_label} | {div_ticker} 배당금 ${amount:.2f} 입금\n"
                           f"💵 현금: ${portfolio['accumulated_cash']:.2f}\n\n{rec_text}")
                    keyboard = {'inline_keyboard': [[
                        {'text': '✅ 매수', 'callback_data': 'div_buy'},
                        {'text': '💵 현금 보유', 'callback_data': 'div_cash'}
                    ]]}
                    user_state[str(chat_id)] = {
                        'mode': 'none', 'cash': portfolio['accumulated_cash'],
                        'rec_account': rec_account, 'rec_ticker': rec_ticker,
                        'rec_shares': rec_shares, 'rec_price': rec_price
                    }
                    send_telegram_msg(chat_id, msg, keyboard)
                else:
                    send_telegram_msg(chat_id, "⚠️ 포트폴리오를 읽을 수 없습니다.")
                    user_state.pop(str(chat_id), None)
            except ValueError:
                send_telegram_msg(chat_id, "숫자만 입력해주세요. 예: 55.20")
            return 'OK'

        return 'OK'

    # ── 콜백 버튼 처리 ──
    callback_query = data.get('callback_query')
    if not callback_query:
        return 'OK'

    callback_data = callback_query.get('data', '')
    callback_id = callback_query['id']
    cb_message = callback_query.get('message', {})
    chat_id = cb_message.get('chat', {}).get('id')
    message_id = cb_message.get('message_id')

    requests.post(f"{bot_url}/answerCallbackQuery", json={'callback_query_id': callback_id})

    if callback_data == 'trade_complete':
        miniapp_url = f"{RENDER_URL}/miniapp"
        keyboard = {'inline_keyboard': [[{
            'text': '📝 체결가 입력',
            'web_app': {'url': miniapp_url}
        }]]}
        requests.post(f"{bot_url}/editMessageReplyMarkup", json={
            'chat_id': chat_id,
            'message_id': message_id,
            'reply_markup': keyboard
        })

    elif callback_data == 'trade_watch':
        # 관망 상태 저장 (재알림용)
        try:
            pending = read_github_file('pending_trades.json')
            if pending:
                save_watch_state(pending)
        except:
            pass
        keyboard = {'inline_keyboard': [[{
            'text': f'👀 관망 중 ({now_str})',
            'callback_data': 'noop'
        }]]}
        requests.post(f"{bot_url}/editMessageReplyMarkup", json={
            'chat_id': chat_id,
            'message_id': message_id,
            'reply_markup': keyboard
        })

    elif callback_data == 'trade_ignore':
        keyboard = {'inline_keyboard': [[{
            'text': f'❌ 무시됨 ({now_str})',
            'callback_data': 'noop'
        }]]}
        requests.post(f"{bot_url}/editMessageReplyMarkup", json={
            'chat_id': chat_id,
            'message_id': message_id,
            'reply_markup': keyboard
        })

    elif callback_data == 'dividend_input':
        # Step 1: TFSA 선택
        keyboard = {'inline_keyboard': [
            [{'text': '💼 TFSA 1', 'callback_data': 'div_select_tfsa1'},
             {'text': '💰 TFSA 2', 'callback_data': 'div_select_tfsa2'}]
        ]}
        send_telegram_msg(chat_id, "💰 배당금 어느 계좌에서 들어왔나요?", keyboard)

    elif callback_data in ('div_select_tfsa1', 'div_select_tfsa2'):
        # Step 2: 종목 선택
        account = 'tfsa1' if callback_data == 'div_select_tfsa1' else 'tfsa2'
        portfolio = read_github_file('current_portfolio.json')
        if portfolio:
            holdings = portfolio.get(account, {})
            buttons = []
            for t in holdings:
                buttons.append([{'text': t, 'callback_data': f'div_ticker_{account}_{t}'}])
            if buttons:
                keyboard = {'inline_keyboard': buttons}
                send_telegram_msg(chat_id, "어떤 종목 배당금인가요?", keyboard)
            else:
                send_telegram_msg(chat_id, "⚠️ 해당 계좌에 보유 자산이 없습니다.")
        else:
            send_telegram_msg(chat_id, "⚠️ 포트폴리오를 읽을 수 없습니다.")

    elif callback_data.startswith('div_ticker_'):
        # Step 3: 금액 입력 대기
        parts = callback_data.split('_', 3)  # div_ticker_tfsa1_XEI.TO
        account = parts[2]
        ticker = parts[3]
        user_state[str(chat_id)] = {
            'mode': 'waiting_dividend_amount',
            'dividend_account': account,
            'dividend_ticker': ticker
        }
        send_telegram_msg(chat_id, f"💰 {ticker} 배당금 금액을 입력하세요.\n예: 55.20\n\n또는 스크린샷을 보내주세요.")

    elif callback_data == 'screenshot_confirm':
        state = user_state.get(str(chat_id), {})
        if state.get('mode') == 'confirm_dividend':
            amount = state.get('amount', 0)
            div_ticker = state.get('ticker')
            portfolio = read_github_file('current_portfolio.json')
            if portfolio:
                old_cash = portfolio.get('accumulated_cash', 0)
                portfolio['accumulated_cash'] = old_cash + amount
                write_github_file('current_portfolio.json', portfolio, '💰 배당금 입금')
                rec_result = get_investment_recommendation(portfolio['accumulated_cash'], div_ticker)
                rec_text, rec_account, rec_ticker, rec_shares, rec_price = rec_result
                msg = f"💰 배당금 ${amount:.2f} 입금\n💵 현금: ${portfolio['accumulated_cash']:.2f}\n\n{rec_text}"
                keyboard = {'inline_keyboard': [[
                    {'text': '✅ 매수', 'callback_data': 'div_buy'},
                    {'text': '💵 현금 보유', 'callback_data': 'div_cash'}
                ]]}
                user_state[str(chat_id)] = {
                    'mode': 'none',
                    'rec_account': rec_account, 'rec_ticker': rec_ticker,
                    'rec_shares': rec_shares, 'rec_price': rec_price
                }
                send_telegram_msg(chat_id, msg, keyboard)

        elif state.get('mode') == 'confirm_trade':
            ticker = state.get('ticker', '')
            action = state.get('action', '')
            shares = state.get('shares', 0)
            price = state.get('price', 0)
            portfolio = read_github_file('current_portfolio.json')
            if portfolio and ticker:
                if action == 'buy':
                    tfsa1 = portfolio.get('tfsa1', {})
                    if ticker in tfsa1:
                        old = tfsa1[ticker]
                        total_shares = old.get('shares', 0) + shares
                        total_cost = old.get('shares', 0) * old.get('avg_price', 0) + shares * price
                        tfsa1[ticker] = {
                            'shares': round(total_shares, 4),
                            'avg_price': round(total_cost / total_shares, 2) if total_shares > 0 else price
                        }
                    else:
                        tfsa1[ticker] = {'shares': shares, 'avg_price': price}
                    portfolio['tfsa1'] = tfsa1
                    # 현금 차감
                    spent = shares * price
                    portfolio['accumulated_cash'] = max(0, portfolio.get('accumulated_cash', 0) - spent)

                elif action == 'sell':
                    tfsa1 = portfolio.get('tfsa1', {})
                    if ticker in tfsa1:
                        old_shares = tfsa1[ticker].get('shares', 0)
                        new_shares = round(old_shares - shares, 4)
                        if new_shares <= 0:
                            del tfsa1[ticker]
                        else:
                            tfsa1[ticker]['shares'] = new_shares
                    portfolio['tfsa1'] = tfsa1
                    # 매도금 현금 추가
                    portfolio['accumulated_cash'] = portfolio.get('accumulated_cash', 0) + shares * price

                write_github_file('current_portfolio.json', portfolio, '📝 체결 반영')
                send_telegram_msg(chat_id, f"✅ 포트폴리오 업데이트 완료\n{ticker} {action} {shares}주 @${price:.2f}")

        user_state.pop(str(chat_id), None)

    elif callback_data == 'screenshot_reject':
        old_state = user_state.get(str(chat_id), {})
        user_state[str(chat_id)] = {
            'mode': 'waiting_dividend_amount',
            'dividend_account': old_state.get('dividend_account', ''),
            'dividend_ticker': old_state.get('ticker', '')
        }
        send_telegram_msg(chat_id, "금액을 직접 입력해주세요.\n예: 55.20")

    elif callback_data == 'div_buy':
        state = user_state.get(str(chat_id), {})
        rec_account = state.get('rec_account')
        rec_ticker = state.get('rec_ticker')
        rec_shares = state.get('rec_shares', 0)
        rec_price = state.get('rec_price', 0)

        portfolio = read_github_file('current_portfolio.json')
        if portfolio and rec_ticker and rec_price > 0:
            cash = portfolio.get('accumulated_cash', 0)
            shares = round(cash / rec_price, 4) if rec_price > 0 else 0

            if rec_account == 'tfsa2':
                # TFSA2 추가매수
                t2 = portfolio.get('tfsa2', {})
                if rec_ticker in t2:
                    old = t2[rec_ticker]
                    total_shares = old.get('shares', 0) + shares
                    total_cost = old.get('shares', 0) * old.get('avg_price', 0) + shares * rec_price
                    t2[rec_ticker]['shares'] = round(total_shares, 4)
                    t2[rec_ticker]['avg_price'] = round(total_cost / total_shares, 2) if total_shares > 0 else rec_price
                else:
                    t2[rec_ticker] = {'shares': shares, 'avg_price': rec_price}
                portfolio['tfsa2'] = t2
            else:
                # TFSA1 매수
                t1 = portfolio.get('tfsa1', {})
                if rec_ticker in t1:
                    old = t1[rec_ticker]
                    total_shares = old.get('shares', 0) + shares
                    total_cost = old.get('shares', 0) * old.get('avg_price', 0) + shares * rec_price
                    t1[rec_ticker] = {
                        'shares': round(total_shares, 4),
                        'avg_price': round(total_cost / total_shares, 2) if total_shares > 0 else rec_price
                    }
                else:
                    t1[rec_ticker] = {'shares': shares, 'avg_price': rec_price}
                portfolio['tfsa1'] = t1

            portfolio['accumulated_cash'] = 0
            write_github_file('current_portfolio.json', portfolio, '💰 배당금 재투자')
            account_label = 'TFSA2' if rec_account == 'tfsa2' else 'TFSA1'
            send_telegram_msg(chat_id, f"✅ {account_label} 매수 완료\n{rec_ticker} {shares}주 @${rec_price:.2f}\n💵 현금: $0")
        else:
            send_telegram_msg(chat_id, "⚠️ 매수할 수 없습니다.")
        user_state.pop(str(chat_id), None)
        keyboard = {'inline_keyboard': [[{'text': f'✅ 매수 완료 ({now_str})', 'callback_data': 'noop'}]]}
        requests.post(f"{bot_url}/editMessageReplyMarkup", json={
            'chat_id': chat_id, 'message_id': message_id, 'reply_markup': keyboard
        })

    elif callback_data == 'div_cash':
        keyboard = {'inline_keyboard': [[{'text': f'💵 현금 보유 ({now_str})', 'callback_data': 'noop'}]]}
        requests.post(f"{bot_url}/editMessageReplyMarkup", json={
            'chat_id': chat_id, 'message_id': message_id, 'reply_markup': keyboard
        })

    return 'OK'


# ============================================================
# 웹훅 설정 (Render 배포 시 자동 설정)
# ============================================================

async def setup_webhook():
    if not RENDER_URL or not RENDER_URL.startswith('https://'):
        print(f"⚠️ 웹훅 설정 건너뜀: RENDER_URL이 유효하지 않음 ({repr(RENDER_URL)})")
        print("   → Render 대시보드에서 RENDER_EXTERNAL_URL 또는 RENDER_URL 환경변수를 확인하세요")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    webhook_url = f"{RENDER_URL}/webhook/{TELEGRAM_TOKEN}"
    await bot.set_webhook(webhook_url)
    print(f"✅ 웹훅 설정 완료: {RENDER_URL}/webhook/[TOKEN HIDDEN]")

# ============================================================
# 메인
# ============================================================

if __name__ == '__main__':
    import asyncio

    # 웹훅 설정
    asyncio.run(setup_webhook())

    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)

