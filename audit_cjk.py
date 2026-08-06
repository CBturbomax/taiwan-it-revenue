#!/usr/bin/env python3
"""audit_cjk.py -- dashboard.html에 남은 한자를 위치별로 분류한다.

화면 문구는 한글로 통일하되, 대만 원자료를 되짚어야 하는 고유명사는 남긴다.
아래 ALLOWED가 그 정식 예외 목록이고, 여기 없는 곳에서 한자가 나오면
FAIL로 보고한다.

    python audit_cjk.py
"""

from __future__ import annotations

import html as H
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "dashboard.html")

# 한자 범위는 코드포인트로만 조립한다. 리터럴 한자로 문자 범위를 적으면
# 경계 문자가 무엇인지 눈으로 확인할 수 없고, 한 글자만 어긋나도 한글
# (U+AC00~U+D7A3)까지 통째로 빨려 들어간다 -- 실제로 그렇게 당했다.
CJK_RANGES = (
    (0x3400, 0x4DBF),   # CJK 확장 A
    (0x4E00, 0x9FFF),   # CJK 통합 한자
    (0xF900, 0xFAFF),   # CJK 호환 한자
)
CJK = re.compile("[" + "".join(f"\\u{a:04x}-\\u{b:04x}"
                               for a, b in CJK_RANGES) + "]")

# 정식 예외 -- 여기 있는 위치의 한자는 의도된 것이다.
ALLOWED = {
    "A. 종목명 툴팁 (한자 원명)":
        "종목명에 마우스를 올렸을 때 뜨는 한자 원명. 대만 자료 검색에 필요.",
    "B. 업종 툴팁 (한자 업종명)":
        "업종 셀 툴팁. MOPS에서 원어 업종명으로 검색해야 할 때가 있다.",
    "D. 모달 헤더 한자명 (SMETA.z)":
        "월별 상세 모달 헤더의 한자 원명. A와 같은 이유.",
}
# API 엔드포인트명(t187ap05_L / mopsfin_t187ap05_O / t21sc03)도 예외지만
# 라틴 문자라 이 검사에는 걸리지 않는다.


def main() -> int:
    if not os.path.exists(PATH):
        print(f"not found: {PATH}", file=sys.stderr)
        return 2
    with open(PATH, encoding="utf-8") as fh:
        doc = fh.read()

    # sanity: the pattern must reject Hangul and accept a Han character
    assert not CJK.search("한글만"), "CJK 패턴이 한글을 잡고 있다"
    assert CJK.search(chr(0x53F0)), "CJK 패턴이 한자를 못 잡는다"

    print(f"dashboard.html {len(doc):,} chars")
    print(f"total CJK characters: {len(CJK.findall(doc)):,}\n")

    buckets: dict[str, Counter] = defaultdict(Counter)

    for m in re.finditer(r'title="([^"]*)"', doc):
        v = H.unescape(m.group(1))
        if not CJK.search(v):
            continue
        tag = doc[doc.rfind("<", 0, m.start()):m.start()][:70]
        if 'class="nm"' in tag:
            buckets["A. 종목명 툴팁 (한자 원명)"][v] += 1
        elif "<td" in tag and "ind" in tag:
            buckets["B. 업종 툴팁 (한자 업종명)"][v] += 1
        else:
            buckets["Z. 분류되지 않은 title 속성"][f"{tag.strip()} -> {v[:50]}"] += 1

    for m in re.finditer(r'"z":"([^"]*)"', doc):
        if CJK.search(m.group(1)):
            buckets["D. 모달 헤더 한자명 (SMETA.z)"][m.group(1)] += 1

    body = re.sub(r"<script\b.*?</script>", "", doc, flags=re.S)
    body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S)
    for m in re.finditer(r">([^<>]+)<", body):
        v = H.unescape(m.group(1)).strip()
        if v and CJK.search(v):
            buckets["Z. 화면에 직접 보이는 텍스트"][v[:80]] += 1

    scripts = "\n".join(re.findall(r"<script\b.*?</script>", doc, flags=re.S))
    for m in re.finditer(r'"([^"]{0,90}?)"', re.sub(r'"z":"[^"]*"', "", scripts)):
        if CJK.search(m.group(1)):
            buckets["Z. 스크립트 내 기타 문자열"][m.group(1)[:80]] += 1

    bad = 0
    for k in sorted(buckets):
        items = buckets[k]
        ok = k in ALLOWED
        if not ok:
            bad += sum(items.values())
        print("=" * 76)
        print(f"{'[예외]' if ok else '[!! 미승인]'} {k}   "
              f"{sum(items.values()):,}건 / 고유 {len(items)}종")
        if ok:
            print(f"    사유: {ALLOWED[k]}")
        print("=" * 76)
        for v, n in items.most_common(10):
            print(f"  x{n:<5} {v}")
        if len(items) > 10:
            print(f"  ... 외 {len(items) - 10}종")
        print()

    print("=" * 76)
    for k in sorted(buckets):
        print(f"  {'OK ' if k in ALLOWED else 'BAD'}  {k}: "
              f"{sum(buckets[k].values()):,}건")
    print("=" * 76)
    print("RESULT:", "PASS -- 정식 예외 위치에만 한자가 남아 있습니다"
          if bad == 0 else f"FAIL -- 미승인 위치에 {bad:,}건")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
