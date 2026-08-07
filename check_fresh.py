#!/usr/bin/env python3
"""check_fresh.py -- 로컬 빌드가 '지금 배포된 것보다 새로운지' 확인한다.

publish.bat은 dashboard.html을 만들지 않고 복사만 한다. 혼자 돌리면 옆에
굴러다니던 파일을 그대로 올려 서버가 방금 만든 대시보드를 덮어쓴다.

처음엔 '파일이 24시간 이내면 통과'로 막으려 했는데 그건 틀린 기준이다.
2.5시간 전에 만든 로컬 빌드도 20분 전 서버 빌드보다는 낡았고, 실제로 그
기준을 통과해 낡은 파일이 두 번 배포됐다. 절대 나이가 아니라 **배포본과의
선후**가 기준이어야 한다.

    python check_fresh.py <파일>
    exit 0 = 로컬이 배포본보다 새로움 (배포해도 됨)
    exit 1 = 낡았거나 판단 불가 (배포하면 안 됨)
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request
from datetime import datetime

LIVE = "https://cbturbomax.github.io/taiwan-it-revenue/"
# build.py는 항상 KST로 찍는다. 그래야 서버(UTC 실행)와 이 PC의 시각을
# 같은 기준으로 비교할 수 있다. 'KST' 꼬리표가 없으면 시간대를 알 수 없는
# 옛 빌드이므로 비교를 거부한다.
STAMP = re.compile(r"갱신\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*KST")
UA = {"User-Agent": "tw-revenue-freshness-check",
      "Cache-Control": "no-cache", "Pragma": "no-cache"}


def stamp_of(text: str):
    m = STAMP.search(text)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_fresh.py <file>")
        return 2
    path = argv[1]
    if not os.path.exists(path):
        print(f"  로컬 빌드 없음: {path}")
        return 1

    with open(path, encoding="utf-8") as fh:
        local = stamp_of(fh.read(200_000))
    if local is None:
        print("  로컬 파일에서 생성 시각을 찾지 못했습니다 -- 배포 중단")
        return 1
    print(f"  로컬 빌드 : {local:%Y-%m-%d %H:%M}")

    try:
        req = urllib.request.Request(f"{LIVE}?freshcheck=1", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            live = stamp_of(r.read(200_000).decode("utf-8", "replace"))
    except Exception as e:
        print(f"  배포본 확인 실패: {type(e).__name__} -- 안전하게 중단합니다")
        return 1

    if live is None:
        print("  배포본에서 생성 시각을 찾지 못했습니다 -- 배포 중단")
        return 1
    print(f"  배포본     : {live:%Y-%m-%d %H:%M}")

    if local > live:
        d = (local - live).total_seconds() / 60
        print(f"  => 로컬이 {d:.0f}분 더 새로움. 배포 가능")
        return 0
    d = (live - local).total_seconds() / 60
    print(f"  => 배포본이 {d:.0f}분 더 새로움. 덮어쓰면 안 됩니다")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
