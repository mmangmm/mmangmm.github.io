"""네이버 블로그 스마트에디터에 글을 입력하고 임시저장한다."""
import os
from playwright.sync_api import Page, sync_playwright

AUTH = "auth_state.json"


def _dismiss_popups(frame, page: Page):
    # '작성 중인 글이 있습니다' 복구 팝업 -> 취소, 도움말 패널 -> 닫기
    for sel in (".se-popup-button-cancel", ".se-help-panel-close-button"):
        try:
            frame.locator(sel).first.click(timeout=3000)
        except Exception:
            pass


def save_draft(title: str, body: str, headless: bool = False, publish: bool = False):
    blog_id = os.environ["NAVER_BLOG_ID"]
    if not os.path.exists(AUTH):
        raise SystemExit("auth_state.json 이 없습니다. 먼저 `python login.py` 를 실행하세요.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=AUTH)
        page = ctx.new_page()
        page.goto(f"https://blog.naver.com/{blog_id}?Redirect=Write", wait_until="domcontentloaded")

        frame = page.frame_locator("iframe#mainFrame")
        frame.locator(".se-documentTitle").wait_for(timeout=30000)
        _dismiss_popups(frame, page)

        # 제목
        frame.locator(".se-documentTitle .se-text-paragraph").first.click()
        page.keyboard.type(title, delay=10)

        # 본문: 첫 본문 영역을 클릭하고 문단 단위로 입력
        frame.locator(".se-component.se-text .se-text-paragraph").first.click()
        for para in body.split("\n"):
            if para.strip():
                page.keyboard.type(para, delay=5)
            page.keyboard.press("Enter")

        # 저장(임시저장) 버튼 - 클래스명은 해시라 텍스트로 찾는다
        frame.get_by_role("button", name="저장", exact=True).click()
        page.wait_for_timeout(2000)
        page.screenshot(path="output/last_draft.png")

        if publish:  # 기본 꺼짐. 완전 자동 발행이 필요할 때만 --publish
            frame.get_by_role("button", name="발행").first.click()
            page.wait_for_timeout(1500)
            frame.get_by_role("button", name="발행").last.click()
            page.wait_for_timeout(3000)
        browser.close()
