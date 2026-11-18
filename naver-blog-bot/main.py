"""topics.txt 의 첫 주제로 글을 생성 -> 네이버 임시저장."""
import argparse
import pathlib
from dotenv import load_dotenv

load_dotenv()
from generate import generate_post  # noqa: E402
from post import save_draft  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--topic", help="topics.txt 대신 직접 주제 지정")
ap.add_argument("--dry-run", action="store_true", help="글만 생성해 output/ 에 저장하고 네이버엔 올리지 않음")
ap.add_argument("--headless", action="store_true")
ap.add_argument("--publish", action="store_true", help="임시저장 대신 바로 발행 (비권장)")
args = ap.parse_args()

pathlib.Path("output").mkdir(exist_ok=True)
topics = [l.strip() for l in open("topics.txt", encoding="utf-8") if l.strip() and not l.startswith("#")]
topic = args.topic or (topics[0] if topics else None)
if not topic:
    raise SystemExit("topics.txt 에 주제가 없습니다.")

title, body = generate_post(topic)
pathlib.Path("output/last_post.txt").write_text(f"{title}\n\n{body}", encoding="utf-8")
print(f"[생성 완료] {title}  (output/last_post.txt)")

if not args.dry_run:
    save_draft(title, body, headless=args.headless, publish=args.publish)
    print("[네이버 임시저장 완료] 블로그 > 글쓰기 > 임시저장 글에서 확인 후 발행하세요.")
    if not args.topic:
        rest = [l for l in open("topics.txt", encoding="utf-8") if l.strip() != topic]
        open("topics.txt", "w", encoding="utf-8").writelines(rest)
        open("done.txt", "a", encoding="utf-8").write(topic + "\n")
