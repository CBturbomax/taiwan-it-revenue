#!/usr/bin/env python3
"""check_ascii.py -- .bat / .cmd 파일에 비ASCII 문자가 있는지 검사한다.

cmd.exe는 배치 파일을 UTF-8이 아니라 시스템 OEM 코드페이지로 읽는다.
한글이 들어가면 멀티바이트가 깨져 주석이나 echo 문구가 명령어로 해석되고,
이런 오류가 쏟아진다:

    '???뜻씩????踰??대 |?샇꽠??'은(는) 내부 또는 외부 명령...
    'op'은(는) 내부 또는 외부 명령...

run.bat이 시작할 때 이걸 자동으로 돌려서, 깨지기 전에 먼저 경고한다.

    python check_ascii.py            # 프로젝트 전체 .bat/.cmd
    python check_ascii.py a.bat b.bat
"""

from __future__ import annotations

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def scan(path: str) -> list[tuple[int, int, str, bytes]]:
    """(줄번호, 열, 줄 미리보기, 문제 바이트) 목록."""
    with open(path, "rb") as fh:
        raw = fh.read()
    bad = []
    line_no, col, line_start = 1, 1, 0
    for i, b in enumerate(raw):
        if b == 0x0A:
            line_no += 1
            col = 1
            line_start = i + 1
            continue
        if b > 0x7F:
            end = raw.find(b"\n", line_start)
            if end < 0:
                end = len(raw)
            preview = raw[line_start:end].decode("utf-8", "replace").rstrip()
            bad.append((line_no, col, preview[:90], bytes([b])))
        col += 1
    return bad


def check_path() -> bool:
    """스크립트가 놓인 경로 자체에 비ASCII가 있는지.

    파일 내용이 깨끗해도 배치 파일이 한글 경로에 있으면 %~dp0 가 한글로
    펼쳐져 같은 증상이 난다. 바탕 화면(OneDrive\\바탕 화면)에 복사해 두고
    실행하는 경우가 대표적이다.
    """
    bad = [c for c in HERE if ord(c) > 0x7F]
    if bad:
        print(f"  BAD  실행 경로에 비ASCII 문자: {HERE}")
        print(f"       문제 문자: {''.join(sorted(set(bad)))}")
        print("       %~dp0 가 이 문자들로 펼쳐져 cmd.exe가 명령어로 오인합니다.")
        print("       배치 파일을 영문 경로(예: C:\\tw-revenue)에서 실행하세요.")
        return False
    print(f"  OK   실행 경로 ASCII: {HERE}")
    return True


def main(argv: list[str]) -> int:
    files = argv[1:]
    path_ok = True
    if not files:
        path_ok = check_path()
        files = sorted(glob.glob(os.path.join(HERE, "*.bat"))
                       + glob.glob(os.path.join(HERE, "*.cmd")))
    if not files:
        print("검사할 .bat / .cmd 파일이 없습니다.")
        return 0 if path_ok else 1

    total_bad = 0
    for path in files:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        bad = scan(path)
        if not bad:
            print(f"  OK   {name:<20} {size:>6,} bytes  (순수 ASCII)")
            continue
        total_bad += 1
        lines = sorted({b[0] for b in bad})
        print(f"  BAD  {name:<20} {size:>6,} bytes  "
              f"비ASCII {len(bad)}바이트, {len(lines)}개 줄: {lines}")
        seen = set()
        for line_no, col, preview, byte in bad:
            if line_no in seen:
                continue
            seen.add(line_no)
            print(f"         L{line_no}: {preview}")

    if total_bad:
        print(f"\n!! {total_bad}개 파일에 비ASCII 문자가 있습니다. "
              f"cmd.exe가 이 줄을 명령어로 오인합니다 -- 로마자로 바꾸세요.")
        return 1
    if not path_ok:
        print(f"\n파일 내용은 모두 ASCII지만 실행 경로가 문제입니다.")
        return 1
    print(f"\n검사한 {len(files)}개 파일 모두 ASCII이고 경로도 정상입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
