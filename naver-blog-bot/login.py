"""최초 1회: 브라우저가 열리면 직접 네이버 로그인(캡차/2단계 포함) 후 Enter.
세션을 auth_state.json 에 저장한다. 자동 입력으로 로그인하면 캡차에 막히기 쉬워서 수동 로그인 방식을 쓴다."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto("https://nid.naver.com/nidlogin.login")
    input("브라우저에서 로그인을 마친 뒤 여기서 Enter를 누르세요... ")
    ctx.storage_state(path="auth_state.json")
    browser.close()
print("auth_state.json 저장 완료 (절대 커밋/공유하지 마세요)")
