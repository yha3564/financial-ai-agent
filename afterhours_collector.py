name: Afterhours Collector

on:
  schedule:
    # ── 16:30~20:00 EST (21:30~01:00 UTC) : 30분마다 ──
    # 장 마감 직후 + 어닝 발표 집중 시간
    - cron: '30 21-23 * * *'   # 21:30, 22:30, 23:30 UTC
    - cron: '0 22-23 * * *'    # 22:00, 23:00 UTC
    - cron: '0,30 0 * * *'     # 00:00, 00:30 UTC
    - cron: '0 1 * * *'        # 01:00 UTC (구간 전환점)

    # ── 20:00~06:00 EST (01:00~11:00 UTC) : 2시간마다 ──
    # 아시아장, 새벽 경제지표 (01:00 포함, 11:00 제외 → 아래 30분 구간에서 처리)
    - cron: '0 3,5,7,9 * * *'

    # ── 06:00~09:00 EST (11:00~14:00 UTC) : 30분마다 ──
    # 유럽장 오픈 + 아침 브리핑 준비
    - cron: '0,30 11-13 * * *'

  workflow_dispatch:

jobs:
  collect:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          token: ${{ secrets.GH_TOKEN }}

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run Afterhours Collector
        env:
          NEWS_API_KEY: ${{ secrets.NEWS_API_KEY }}
        run: python afterhours_collector.py

      - name: Commit afterhours_news.json
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add afterhours_news.json seen_afterhours.json || true
          git diff --staged --quiet || git commit -m "📰 장후 뉴스 업데이트 $(date -u '+%Y-%m-%d %H:%M UTC')"
          git push
