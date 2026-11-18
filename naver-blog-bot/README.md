# 네이버 블로그 자동 임시저장 봇

Claude API로 글 생성 → Playwright로 네이버 스마트에디터에 입력 → **임시저장**(발행은 직접).

## 설치 (내 PC)
```bash
cd naver-blog-bot
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # ANTHROPIC_API_KEY, NAVER_BLOG_ID 입력
python login.py        # 최초 1회: 브라우저에서 직접 로그인 후 Enter
```

## 사용
```bash
python main.py --dry-run          # 글만 생성해서 output/last_post.txt 로 확인
python main.py                    # topics.txt 첫 주제 -> 임시저장
python main.py --topic "주제"     # 주제 직접 지정
```
정기 실행은 cron / 작업 스케줄러에 `python main.py --headless` 를 등록하세요.

## 주의
- 네이버 글쓰기 API는 종료되어 UI 자동화에 의존합니다. 에디터 개편 시 `post.py` 의 선택자를 고쳐야 합니다.
- 네이버 약관/이상 접근 탐지로 제한될 수 있으니 하루 글 수를 적게 유지하세요.
- `.env`, `auth_state.json` 은 .gitignore 처리됨. 이 저장소는 공개이므로 절대 커밋하지 마세요.
