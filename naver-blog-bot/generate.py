"""Claude API로 블로그 글(제목+본문)을 생성한다."""
import os
import anthropic

MODEL = "claude-sonnet-5-5"

PROMPT = """네이버 블로그에 올릴 글을 작성해줘.

주제: {topic}
작성자 설정: {persona}

규칙:
- 첫 줄은 반드시 `TITLE: ` 로 시작하는 제목 한 줄 (검색에 잘 걸리는 자연스러운 제목, 40자 이내)
- 한 줄 띄우고 본문. 마크다운 기호(#, *, - 등)는 쓰지 말고 일반 텍스트만 사용
- 소제목은 그냥 한 줄짜리 문장으로, 문단은 빈 줄로 구분
- 도입 → 본문 3~4개 소제목 → 마무리 구조, 분량 1500~2000자
- 확인할 수 없는 가격·영업시간·수치는 지어내지 말고 '방문 전 확인 필요'로 표기
"""


def generate_post(topic: str) -> tuple[str, str]:
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
    persona = os.getenv("BLOG_PERSONA", "친근한 말투의 블로거")
    msg = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": PROMPT.format(topic=topic, persona=persona)}],
    )
    text = msg.content[0].text.strip()
    first, _, rest = text.partition("\n")
    if not first.startswith("TITLE:"):
        raise ValueError(f"제목 형식이 올바르지 않습니다: {first!r}")
    return first.removeprefix("TITLE:").strip(), rest.strip()
