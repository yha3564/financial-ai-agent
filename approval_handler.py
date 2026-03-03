import os
import yaml
import requests
from datetime import datetime, timedelta
from telegram import Bot
import asyncio
import pytz
import re

class ApprovalHandler:
    def __init__(self):
        self.telegram_token = os.environ['TELEGRAM_BOT_TOKEN']
        self.telegram_chat_id = os.environ['TELEGRAM_CHAT_ID']
        self.github_token = os.environ.get('GH_TOKEN', '')
        repo_full = os.environ.get('GITHUB_REPOSITORY', '')
        if '/' in repo_full:
            self.repo_owner, self.repo_name = repo_full.split('/')
        else:
            self.repo_owner, self.repo_name = '', ''
        
        self.toronto_tz = pytz.timezone('America/Toronto')
        
        # 포트폴리오 로드
        with open('portfolio.yaml', 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
    
    async def check_messages(self):
        """최근 텔레그램 메시지 확인"""
        bot = Bot(token=self.telegram_token)
        
        try:
            # 최근 24시간 메시지만 확인
            updates = await bot.get_updates(limit=10)
            
            for update in updates:
                if update.message and update.message.chat.id == int(self.telegram_chat_id):
                    message_time = update.message.date
                    now = datetime.now(pytz.UTC)
                    
                    # 24시간 이내 메시지만
                    if (now - message_time).total_seconds() < 86400:
                        text = update.message.text
                        if text and text.startswith('승인'):
                            await self.process_approval(text, bot)
        
        except Exception as e:
            print(f"메시지 확인 오류: {e}")
    
    async def process_approval(self, message, bot):
        """승인 메시지 처리"""
        print(f"승인 메시지 발견: {message}")
        
        # 메시지 파싱: "승인 HXQ 50" 또는 "승인 ZAG"
        parts = message.strip().split()
        
        if len(parts) < 2:
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text="❌ 형식 오류\n올바른 형식:\n승인 [티커]\n승인 [티커] [비율]"
            )
            return
        
        ticker = parts[1].upper()
        percentage = 100  # 기본값
        
        if len(parts) >= 3:
            try:
                percentage = int(parts[2])
            except:
                percentage = 100
        
        # 티커 정규화 (.TO 추가)
        if not ticker.endswith('.TO') and ticker not in ['VOO', 'QQQM', 'XLE', 'NVDA']:
            ticker_with_to = f"{ticker}.TO"
        else:
            ticker_with_to = ticker
        
        # 어느 자산을 교체할지 찾기
        old_asset = self.find_asset_to_replace(ticker_with_to)
        
        if not old_asset:
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=f"❌ {ticker} 처리 실패\n최근 추천된 종목인지 확인하세요."
            )
            return
        
        # GitHub 업데이트
        success = self.update_portfolio(old_asset, ticker_with_to, percentage)
        
        if success:
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=f"✅ 포트폴리오 업데이트 완료!\n\n"
                     f"계좌: {old_asset['account']}\n"
                     f"변경: {old_asset['name']} → {ticker}\n"
                     f"비율: {percentage}%\n\n"
                     f"다음 실행부터 {ticker} 추적 시작!"
            )
        else:
            await bot.send_message(
                chat_id=self.telegram_chat_id,
                text=f"❌ GitHub 업데이트 실패\n로그를 확인하세요."
            )
    
    def find_asset_to_replace(self, new_ticker):
        """교체할 자산 찾기 (최근 추천 기록에서)"""
        # 간단한 로직: 마지막 추천된 자산
        # 실제로는 더 정교한 매칭 필요
        
        # TFSA1에서 찾기
        for asset in self.config.get('tfsa1_assets', []):
            # 임시: 첫 번째 자산 반환 (나중에 개선)
            return {
                'account': 'TFSA1',
                'ticker': asset['ticker'],
                'name': asset['name']
            }
        
        # TFSA2에서 찾기
        for asset in self.config.get('tfsa2_assets', []):
            return {
                'account': 'TFSA2',
                'ticker': asset['ticker'],
                'name': asset['name']
            }
        
        return None
    
    def update_portfolio(self, old_asset, new_ticker, percentage):
        """GitHub API로 portfolio.yaml 업데이트"""
        if not self.github_token or not self.repo_owner or not self.repo_name:
            print("GitHub 정보 부족")
            return False
        
        try:
            # 현재 파일 가져오기
            url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/contents/portfolio.yaml"
            headers = {
                'Authorization': f'token {self.github_token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                print(f"파일 가져오기 실패: {response.status_code}")
                return False
            
            file_data = response.json()
            sha = file_data['sha']
            
            # 새 자산 정보 생성
            new_asset_data = self.create_asset_entry(new_ticker, old_asset['account'])
            
            # YAML 수정
            if old_asset['account'] == 'TFSA1':
                # TFSA1: 기존 자산 유지하고 새 자산 추가
                if percentage < 100:
                    self.config['tfsa1_assets'].append(new_asset_data)
                else:
                    # 100%면 교체
                    self.config['tfsa1_assets'] = [
                        a for a in self.config['tfsa1_assets'] 
                        if a['ticker'] != old_asset['ticker']
                    ]
                    self.config['tfsa1_assets'].append(new_asset_data)
            
            elif old_asset['account'] == 'TFSA2':
                # TFSA2: 무조건 전량 교체
                old_purpose = None
                old_contribution = None
                old_target = None
                
                for asset in self.config['tfsa2_assets']:
                    if asset['ticker'] == old_asset['ticker']:
                        old_purpose = asset.get('purpose', '')
                        old_contribution = asset.get('monthly_contribution', 0)
                        old_target = asset.get('target_amount', 0)
                        break
                
                # 기존 자산 제거
                self.config['tfsa2_assets'] = [
                    a for a in self.config['tfsa2_assets'] 
                    if a['ticker'] != old_asset['ticker']
                ]
                
                # 목적 정보 유지
                if old_purpose:
                    new_asset_data['purpose'] = old_purpose
                if old_contribution:
                    new_asset_data['monthly_contribution'] = old_contribution
                if old_target:
                    new_asset_data['target_amount'] = old_target
                
                self.config['tfsa2_assets'].append(new_asset_data)
            
            # YAML 문자열로 변환
            import base64
            new_content = yaml.dump(self.config, allow_unicode=True, sort_keys=False)
            encoded_content = base64.b64encode(new_content.encode()).decode()
            
            # GitHub에 커밋
            commit_message = f"[자동] {old_asset['account']}: {old_asset['ticker']} → {new_ticker} ({percentage}%)"
            
            update_data = {
                'message': commit_message,
                'content': encoded_content,
                'sha': sha
            }
            
            response = requests.put(url, json=update_data, headers=headers)
            
            if response.status_code == 200:
                print(f"✅ GitHub 업데이트 성공!")
                return True
            else:
                print(f"❌ GitHub 업데이트 실패: {response.status_code}")
                print(response.text)
                return False
        
        except Exception as e:
            print(f"업데이트 오류: {e}")
            return False
    
    def create_asset_entry(self, ticker, account):
        """새 자산 엔트리 생성"""
        # 티커에서 이름 추정
        name_map = {
            'HXQ.TO': 'HXQ - NASDAQ Hedged',
            'XLE': 'XLE - Energy Sector',
            'VOO': 'VOO - S&P500',
            'VFV.TO': 'VFV - S&P500 CAD',
            'ZAG.TO': 'ZAG - Canadian Bonds',
            'VSB.TO': 'VSB - Short Term Bonds',
            'CASH.TO': 'CASH - High Interest',
        }
        
        name = name_map.get(ticker, ticker)
        
        base_entry = {
            'name': name,
            'ticker': ticker,
            'account': account,
            'keywords': [ticker.replace('.TO', '')]
        }
        
        if account == 'TFSA1':
            base_entry.update({
                'strategy': 'aggressive',
                'risk_tolerance': 'high',
                'trading_style': 'active'
            })
        else:
            base_entry.update({
                'strategy': 'conservative',
                'risk_tolerance': 'low',
                'trading_style': 'all_in_only'
            })
        
        return base_entry
    
    def run(self):
        """메인 실행"""
        print("🔍 텔레그램 승인 메시지 확인 중...")
        asyncio.run(self.check_messages())
        print("✅ 완료!")

if __name__ == "__main__":
    handler = ApprovalHandler()
    handler.run()
