"""logo.py -- GENERATED. h1 옆에 붙는 아이콘.

원본 cb.png는 확장자와 달리 실제로는 640x640 WebP이고 배경이 흰색이라,
그대로 넣으면 어두운 헤더 위에 흰 사각형이 뜬다.

배경 판정은 밝기가 아니라 '프레임 가장자리에서 연결된 흰 영역'으로 한다.
밝기만 보면 이모지 이마의 하이라이트(최소채널 234까지 올라감)까지 배경으로
오인해 얼굴에 구멍이 뚫린다. 하이라이트는 얼굴에 둘러싸여 가장자리와
이어지지 않으므로 flood fill 방식에서는 안전하다.

임계값 최소채널 232 이상 + 가장자리 연결 -> 알파 0
삽입 크기 3,740 bytes (image/png, 40x40)
"""

LOGO_DATA_URI = (
    "data:image/png;base64,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"
    "VORK5CYII="
)
