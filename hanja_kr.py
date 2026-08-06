"""hanja_kr.py -- 한자 -> 한국어 음독 변환 (오프라인, 의존성 없음).

`hanja` 패키지가 Python 3.14에서 설치·import 모두 실패하므로, 종목명에 실제로
쓰인 한자만 직접 표로 갖는다. 대만 상장 IT 948종목의 종목명에 등장하는 고유
한자는 593자이며 아래 표가 그 전부다.

두음법칙을 적용한다. 이게 없으면 聯發科가 "련발과", 林이 "림", 龍이 "룡"으로
나와 한국어로 읽히지 않는다. 표에는 본음(ㄹ/ㄴ 초성)을 적고, 이름의 첫 음절일
때만 규칙을 적용한다.

새 한자가 나오면 `python gen_names_auto.py`가 미매핑 문자를 목록으로 알려준다.
"""

from __future__ import annotations

import re

# 한자+음독을 공백으로 구분. 한 줄 60자씩, 코드포인트 정렬 순서.
_TABLE = """
一일 三삼 上상 世세 丞승 中중 久구 乙을 九구 事사 二이 云운 互호 井정 亞아
亨형 京경 人인 仁인 今금 仲중 伊이 伍오 伯백 伸신 位위 佑우 你니 佰백 佳가
佶길 來래 俊준 保보 信신 倉창 倍배 倚의 倫륜 偉위 健건 偶우 傑걸 傳전 像상
僑교 儀의 億억 優우 元원 兆조 先선 光광 克극 全전 公공 其기 典전 再재 冠관
凌릉 凡범 凱개 利리 前전 剛강 創창 力력 加가 勁경 勉면 動동 務무 勝승 勤근
匯회 十십 千천 半반 協협 南남 博박 印인 原원 及급 友우 叡예 可가 台태 合합
吉길 同동 名명 君군 呈정 和화 品품 哲철 唐당 售수 商상 啟계 喬교 單단 嘉가
四사 固고 國국 園원 圓원 土토 均균 坤곤 城성 基기 堂당 堡보 塑소 塚총 增증
士사 壹일 大대 天천 太태 奇기 奕혁 奧오 好호 如여 威위 媒매 子자 字자 孚부
學학 宇우 安안 宏홍 定정 宜의 宣선 家가 宸신 容용 密밀 富부 實실 寧녕 寰환
寶보 導도 尖첨 尚상 尬개 尼니 居거 展전 屬속 山산 岱대 岳악 峰봉 崇숭 崗강
崧숭 崴외 嶠교 川천 州주 工공 巧교 巨거 帆범 希희 帝제 常상 幃위 平평 幸행
序서 康강 廣광 建건 式식 弘홍 強강 彩채 彰창 律률 得득 復복 微미 德덕 徽휘
心심 必필 志지 快쾌 思사 恩은 恬념 悅열 惟유 惠혜 意의 愛애 慧혜 慶경 憶억
應응 懋무 成성 戲희 所소 承승 技기 投투 拓척 振진 捷첩 探탐 揚양 撼감
擎경 攸유 敏민 敘서 敦돈 敬경 敵적 數수 文문 料료 新신 方방 日일 旦단 旭욱
控공
旺왕 昆곤 昇승 昌창 明명 易이 昕흔 星성 映영 是시 昱욱 昶창 晉진 晟성 晨신
普보 景경 晶정 智지 暉휘 暐위 暘양 曄엽 曜요 月월 有유 朋붕 服복 望망 李이
材재 杭항 杰걸 東동 松송 板판 林림 柏백 格격 梓재 梭사 森삼 椿춘 楠남 業업
極극 榮영 樺화 橋교 橙등 機기 橡상 欣흔 歐구 正정 毅의 比비 永영 汎범 沛패
泉천 泓홍 波파 泰태 洋양 洲주 浦포 浪랑 海해 淩릉 淳순 測측 港항 湖호 湛담
湧용 湯탕 準준 漢한 潤윤 澤택 濟제 濱빈 濾려 瀚한 灣만 炫현 為위 無무 焱염
照조 熒형 熱열 熹희 營영 燦찬 燿요 爾이 牧목 特특 率률 玉옥 玖구 珵정 現현
球구 琦기 琪기 瑋위 瑞서 瑪마 璟경 環환 生생 田전 由유 甲갑 界계 異이 登등
發발 百백 的적 皓호 盈영 益익 盛성 盟맹 相상 盾순 眾중 瞻첨 石석 矽석 研연
碁기 碩석 磊뢰 磐반 神신 祥상 祺기 福복 禧희 禾화 科과 程정 稜릉 積적 穎영
穩온 空공 立립 竑홍 端단 競경 竹죽 笙생 笛적 策책 範범 米미 精정 系계 納납
紘홍 統통 經경 綜종 綠록 維유 綱강 網망 綸륜 緯위 總총 罩조 羅라 美미 群군
義의 翊익 翔상 翰한 耀요 而이 耕경 耘운 聖성 聚취 聯련 聰총 聲성 股고 育육
胡호 能능 腦뇌 至지 致치 臺대 臻진 興흥 舒서 舜순 舟주 航항 良량 艾애 芯심
若약 英영 茂무 荃전 華화 菱릉 菲비 萊래 萬만 葆보 蒙몽 蔚위 藍람 藝예 虎호
虹홍 蜜밀 融융 行행 衛위 表표 裕유 西서 見견 觀관 訊신 訓훈 詠영 詮전 誠성
譁화 譜보 谷곡 豐풍 豪호 豫예 買매 貿무 資자 賢현 賽새 赫혁 超초 越월 路로
車차 軒헌 軟연 輔보 輝휘 辛신 辰진 迅신 迎영 迪적 通통 速속 連련 進진 逸일
遊유 運운 道도 達달 遠원 邁매 邑읍 邦방 郡군 酷혹 采채 金금 針침 鈞균 鈦태
鈺옥 鉅거 鉞월 銓전 銘명 銳예 鋐홍 鋒봉 錠정 錡기 錦금 錩창 錸래 錼내 鎧개
鎰일 鏵화 鑫흠 鑽찬 長장 門문 閎홍 關관 附부 陞승 陽양 隆륭 隊대 際제 隴롱
隼준 雅아 集집 雍옹 雙쌍 雨우 雲운 零령 雷뢰 電전 震진 霖림 青청 韋위 音음
韻운 順순 頌송 頎기 領령 頡힐 類류 顧고 風풍 飛비 首수 馳치 騰등 驊화 體체
高고 鴻홍 鵬붕 麗려 點점 鼎정 齊제 龍룡
"""


def _load() -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in _TABLE.split():
        if len(tok) < 2:
            continue
        out[tok[0]] = tok[1:]
    return out


HANJA: dict[str, str] = _load()

CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# --- 두음법칙 (word-initial only) ------------------------------------------- #
_SBASE, _LCOUNT, _VCOUNT, _TCOUNT = 0xAC00, 19, 21, 28
_L_R, _L_N, _L_NG = 5, 2, 11              # ㄹ, ㄴ, ㅇ
_YOTED = {2, 6, 7, 12, 17, 20}            # ㅑ ㅕ ㅖ ㅛ ㅠ ㅣ


def _decompose(ch: str):
    i = ord(ch) - _SBASE
    if not 0 <= i < _LCOUNT * _VCOUNT * _TCOUNT:
        return None
    return i // (_VCOUNT * _TCOUNT), (i // _TCOUNT) % _VCOUNT, i % _TCOUNT


def _compose(l: int, v: int, t: int) -> str:
    return chr(_SBASE + (l * _VCOUNT + v) * _TCOUNT + t)


def initial_sound_rule(syl: str) -> str:
    """려->여, 료->요, 리->이, 라->나, 로->노, 뢰->뇌, 녀->여, 니->이 ...

    Applied only to the first syllable of a name, which is where Korean
    orthography drops the ㄹ/ㄴ onset.
    """
    d = _decompose(syl)
    if d is None:
        return syl
    l, v, t = d
    if l == _L_R:
        return _compose(_L_NG if v in _YOTED else _L_N, v, t)
    if l == _L_N and v in _YOTED:
        return _compose(_L_NG, v, t)
    return syl


def to_korean(name: str) -> tuple[str, list[str]]:
    """한자 -> 음독. 비한자(라틴/기호/-KY 등)는 그대로 통과.

    Returns (reading, unmapped_chars).
    """
    out: list[str] = []
    missing: list[str] = []
    for ch in name:
        if CJK.match(ch):
            r = HANJA.get(ch)
            if r is None:
                missing.append(ch)
                out.append(ch)          # leave it visible rather than silently drop
            else:
                out.append(r)
        else:
            out.append(ch)
    s = "".join(out)
    # the rule applies to the first syllable only, and only if the name starts
    # with a converted hanja (not with "TPK-KY" or "91APP")
    if s and CJK.match(name[0]):
        s = initial_sound_rule(s[0]) + s[1:]
    return s, missing


if __name__ == "__main__":
    print(f"HANJA table: {len(HANJA)} characters")
    for t in ("明泰", "德律", "牧德", "台積電", "鴻海", "廣達", "聯發科", "聯電",
              "林口", "龍巖", "羅昇", "利機", "綠能", "臻鼎-KY", "TPK-KY",
              "91APP*-KY", "大鵬科CLMX"):
        r, miss = to_korean(t)
        flag = f"   MISSING {miss}" if miss else ""
        print(f"  {t:<12} -> {r}{flag}")
