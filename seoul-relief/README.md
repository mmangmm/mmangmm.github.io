# Seoul relief map generator

GPX 경로 → 25×25cm 3D 프린트용 STL (지형 + 높은 건물 위주로 단순화·과장 + 빨간 경로 + 기록 문구).

```bash
pip install -r requirements.txt

# 오프라인 샘플 (가짜 코스/데이터, 파이프라인 확인용)
python make_relief.py --demo --out out_demo --line "JTBC SEOUL MARATHON 2026" --line "42.195 km  3:45:12"

# 실제: 내 달리기 GPX + OpenStreetMap 건물/한강 + SRTM 지형 (인터넷 필요)
python make_relief.py --gpx my_run.gpx --out out \
    --line "JTBC SEOUL MARATHON 2026" --line "홍길동 · 42.195 km · 3:45:12" --font NanumGothicBold.ttf
```

출력: `body.stl`(흰색) · `route.stl`(빨강, 몸체에 0.3mm 겹침) · `text.stl`(문구) · `preview.png`.
슬라이서에서 세 파일을 한 오브젝트의 파트로 불러 필라멘트만 다르게 지정한다 (좌표가 동일).

## 건물 단순화 / 높이 과장 파라미터
| 옵션 | 기본 | 의미 |
|---|---|---|
| `--min-height` | 40 m | 이보다 낮은 건물은 버림 |
| `--max-height` | 300 m | 이 이상은 같은 높이로 캡 (롯데타워 등) |
| `--b-min` / `--b-max` | 1 / 14 mm | 최저·최고 건물의 출력 높이 |
| `--gamma` | 0.8 | 작을수록 중간 높이도 올라옴, 클수록 초고층만 도드라짐 |
| `--min-width` | 1.2 mm | 이보다 얇은 건물은 키워서 이웃과 병합 |
| `--terrain-exag` | 2.5 | 지형 과장 배율 |

시편을 `--size 80` 정도로 작게 뽑아 배율을 정한 뒤 250으로 올리는 것을 추천.

## 데이터 소스
- 건물/물: OpenStreetMap (Overpass). **ODbL — 판매 시 출처 표기 필요.** 국토부/V-World 건물 데이터는 `--buildings file.geojson`(lon/lat)으로 대체 가능.
- 지형: OpenTopoData SRTM 30m (공개 API는 1회/초 제한이라 처음 한 번 수 분 걸리고 `out/cache`에 저장됨).

## 테스트
`python -m unittest test_relief`
