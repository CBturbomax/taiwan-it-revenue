"""bom_groups.py -- AI 서버 BoM(자재명세) 기준 종목 그룹.

대만 공식 업종분류(半導體業 / 電子零組件業 ...)는 AI 서버 밸류체인을 보여주지
못한다. 台達電(파워)과 川湖(랙 슬라이드레일)이 똑같이 電子零組件業으로 묶이는
식이다. 이 파일은 그 대신 "AI 서버 한 대를 만드는 데 어느 층인가"로 묶는다.

EDIT THIS FILE BY HAND. build.py가 읽기만 하고 절대 덮어쓰지 않는다.
GROUPS의 선언 순서가 대시보드 렌더 순서다.

★ 업종 필터 우회
  여기 들어간 코드는 groups.IT_INDUSTRIES와 무관하게 무조건 DB에서 가져온다.
  8996 高力(KAORI, 판형 열교환기 -> 데이터센터 액냉 CDU)가 電機機械業으로
  분류되어 IT 필터에 안 걸리는 것이 대표적인 이유다. 현재 우회가 실제로
  필요한 종목은 8996 하나이며, 새 코드를 넣을 때 업종은 신경 쓰지 않아도 된다.

★ 종목 추가 시 반드시 확인할 것
  같은 그룹에 이미 있는 종목과 모자관계인지. 연결재무제표 기준 매출이라
  모회사 매출에 자회사가 이미 포함되어 있고, 그대로 더하면 그룹 합계가
  부풀려진다. 발견하면 CONSOL_PARENT에 등록한다.
"""

from __future__ import annotations

GROUPS: dict[str, list[str]] = {
    # ---- 전공정 ----------------------------------------------------------- #
    "AI 파운드리": ["2330"],
    "Mature 파운드리": ["2303", "5347", "6770"],
    "OSAT/패키징": ["3711", "6239", "8150", "6257", "2329", "2441"],
    "메모리": ["2408", "2344", "2337", "3006", "8299", "5351", "6531"],
    "메모리 모듈/스토리지": ["3260", "2451", "5289", "8271", "4967", "4973"],
    # ---- 시스템 조립 ------------------------------------------------------- #
    "서버 ODM": ["2317", "2382", "3231", "6669", "2356", "2376", "7711"],
    "후발 ODM": ["2324", "4938", "3706"],
    "네트워크/스위치": ["2345", "3380", "6285"],
    # ---- 기판 / 소재 ------------------------------------------------------- #
    # ABF 기판(IC 서브스트레이트)과 CCL(동박적층판)은 BoM상 다른 층이라 분리.
    "ABF 기판": ["3037", "3189", "8046"],
    "CCL·소재": ["2383", "6213", "6274", "1815", "8358"],
    "서버/AI PCB": ["2368", "3044", "2313", "3715", "5469"],
    # ---- 전력 / 열 --------------------------------------------------------- #
    "파워 (PSU/VRM)": ["2308", "6412", "3015", "6282", "2301"],
    "쿨링 (액냉/공랭)": ["3017", "2421", "3324", "8996", "3653", "6230", "3338"],
    # ---- 기구 / 부품 ------------------------------------------------------- #
    "커넥터/레일/섀시": ["2059", "3533", "6805", "8210", "3665"],
    "패시브 부품": ["2327", "2492", "3026"],
    "리드프레임": ["2351", "6548"],
    # ---- 칩 / 검사 --------------------------------------------------------- #
    "BMC/서버IP/ASIC": ["5274", "3661", "3443", "6533", "4966", "3035", "5269"],
    "테스트/프루브/소켓": ["6515", "6223", "6510", "2449", "7769", "2360"],
    # ---- 광 / 화합물 (GaAs는 광통신 트랜시버 상류라 붙여 배치) ---------------- #
    "광통신": ["4979", "3234", "6442", "3081", "3163", "3450"],
    "화합물반도체(GaAs)": ["3105", "4991", "8086"],
    # ---- 장비 / 유통 ------------------------------------------------------- #
    "장비/공정소재": ["3680", "3131", "3583", "6187", "5483", "6488", "6182",
                  "8021", "3413", "1785"],
    "팹리스/IC설계": ["2454", "2379", "3034", "3529", "6526"],
    "전자유통": ["3702", "3036", "2347", "8112", "8096"],
}

# 자회사 -> 모회사. 모회사가 연결재무제표에 자회사 매출을 이미 포함하므로,
# 히트맵의 그룹 합계에서 자회사(키)를 제외한다. 모회사가 같은 그룹에 없으면
# 제외하지 않는다 -- 그때는 그룹 합계에 이중계상이 발생하지 않기 때문이다.
# 차트와 표에는 자회사도 그대로 노출된다.
#
# 등록 기준: 모회사가 자회사를 "연결"하는 경우만.
# 지분법(50% 미만, 관계기업) 관계는 매출이 합산되지 않으므로 등록하지 않는다.
CONSOL_PARENT: dict[str, str] = {
    "6669": "3231",   # 緯穎 ⊂ 緯創      -- 둘 다 서버 ODM. 활성
    "6488": "5483",   # 環球晶 ⊂ 中美晶  -- 둘 다 장비/공정소재. 활성
    # 연결관계는 맞지만 모회사가 다른 그룹이라 현재 비활성. 그룹을 옮기면
    # 되살아나므로 미리 적어둔다.
    # "3413": "2317",  # 京鼎 ⊂ 鴻海   폭스콘 계열. 장비/공정소재 vs 서버 ODM
    # "6510": "2313",  # 精測 ⊂ 華通   華通 분할 계열. 테스트 vs 서버/AI PCB
    # "7711": "3515",  # 永擎 ⊂ 華擎   3515는 그룹에 없음
    # "5269": "2357",  # 祥碩 ⊂ 華碩   2357은 그룹에 없음
    #
    # 3035 智原 / 2303 聯電은 지분법 관계이므로 등록 대상이 아니다.
}

# 종목별 한 줄 설명. 빈 값이거나 키가 없으면 폴백 문구가 들어간다.
BIZ: dict[str, str] = {
    # --- AI 파운드리 ---
    "2330": "세계 1위 파운드리(반도체 위탁생산)",
    # --- Mature 파운드리 ---
    "2303": "성숙공정 파운드리 세계 3위, 28nm 이상 중심",
    "5347": "8인치 성숙공정 파운드리, 아날로그·PMIC 위탁생산",
    "6770": "DRAM과 성숙공정 파운드리 겸업, 12인치 팹",
    # --- OSAT/패키징 ---
    "3711": "세계 1위 OSAT(후공정 패키징·테스트), ASE+SPIL 통합지주",
    "6239": "메모리 패키징·테스트 전문 OSAT",
    "8150": "메모리·디스플레이 드라이버IC 패키징·테스트",
    "6257": "IC 테스트·패키징 OSAT, 고주파 비중",
    "2329": "메모리·로직 패키징 OSAT",
    "2441": "리드프레임 기반 패키징·테스트 OSAT",
    # --- 메모리 ---
    "2408": "대만 최대 DRAM 메이커, 포모사그룹 계열",
    "2344": "니치 DRAM과 NOR 플래시 종합 메모리",
    "2337": "NOR 플래시 세계 선두, ROM·니치 메모리",
    "3006": "니치 DRAM·SRAM 팹리스 메모리",
    "8299": "NAND 컨트롤러 세계 1위, SSD 솔루션",
    "5351": "니치 DRAM·버퍼 메모리 팹리스",
    "6531": "커스텀 DRAM·PSRAM 팹리스",
    # --- 메모리 모듈/스토리지 ---
    "3260": "메모리 모듈·SSD 브랜드",
    "2451": "메모리 모듈·산업용 스토리지 브랜드",
    "5289": "산업용 SSD·임베디드 스토리지",
    "8271": "산업용·컨슈머 메모리 모듈",
    "4967": "메모리 모듈·SSD 브랜드",
    "4973": "메모리 모듈·외장 스토리지 브랜드",
    # --- 서버 ODM ---
    "2317": "세계 최대 EMS, AI서버 랙 조립 최대 수혜",
    "2382": "노트북·AI서버 ODM, 엔비디아 랙 시스템 주력",
    "3231": "노트북·AI서버 ODM, GPU 보드·랙",
    "6669": "하이퍼스케일러 전용 서버 ODM(위스트론 계열)",
    "2356": "노트북·서버 ODM, AI서버 비중 확대",
    "2376": "메인보드·그래픽카드 브랜드, Giga Computing으로 AI서버 제조",
    "7711": "서버 마더보드·랙 시스템(애즈락 ASRock 계열)",
    # --- 후발 ODM ---
    "2324": "노트북 ODM 2위, 서버 신규 진입",
    "4938": "애플 조립 EMS, 서버·전장 확대",
    "3706": "서버·산업용 컴퓨팅 ODM(MiTAC 그룹)",
    # --- 네트워크/스위치 ---
    "2345": "데이터센터 화이트박스 스위치 ODM 세계 1위",
    "3380": "네트워크 장비 ODM, 스위치·게이트웨이",
    "6285": "무선 네트워크·차량용 통신 모듈 ODM",
    # --- ABF 기판 ---
    "3037": "ABF 기판 세계 선두, IC 서브스트레이트",
    "3189": "IC 기판(ABF·BT), 플립칩 서브스트레이트",
    "8046": "ABF 기판·IC 서브스트레이트, 포모사그룹 계열",
    # --- CCL·소재 ---
    "2383": "CCL 세계 선두, AI서버용 저손실 소재",
    "6213": "동박적층판(CCL), 고속·저손실 소재",
    "6274": "고주파·저손실 CCL, AI서버용 소재",
    "1815": "로우Dk 유리섬유 원사, CCL 상류 소재",
    "8358": "전해동박, CCL 상류 소재",
    # --- 서버/AI PCB ---
    "2368": "서버용 다층 PCB, AI서버 메인보드",
    "3044": "다층 PCB·HDI, 서버·차량용",
    "2313": "HDI·PCB, 서버·모바일용",
    "3715": "차량용·서버용 다층 PCB(HDI) 지주사",
    "5469": "다층 PCB, 서버·메모리 모듈용",
    # --- 파워 (PSU/VRM) ---
    "2308": "전원·전력관리 세계 1위, AI서버 PSU·전력랙",
    "6412": "서버·PC 전원공급장치(PSU)",
    "3015": "PSU 전문, 서버·산업용 전원",
    "6282": "서버·통신용 전원공급장치",
    "2301": "서버·데이터센터 PSU, 델타(2308)에 이어 2위 규모",
    # --- 쿨링 (액냉/공랭) ---
    "3017": "서버 방열·팬·액냉 솔루션, AI서버 쿨링 핵심",
    "2421": "냉각팬·열관리 모듈",
    "3324": "히트싱크·액냉 콜드플레이트",
    "8996": "판형 열교환기, 데이터센터 액냉 CDU 부품",
    "3653": "방열판·베이퍼챔버, 파워반도체 방열",
    "6230": "히트파이프·히트싱크, 니덱(Nidec) 그룹",
    "3338": "히트파이프·방열 모듈",
    # --- 커넥터/레일/섀시 ---
    "2059": "서버 랙 슬라이드레일(엔비디아 공급망)",
    "3533": "CPU 소켓·고속 커넥터",
    "6805": "힌지·커넥터, 폴더블·서버 부품",
    "8210": "서버 섀시·랙 인클로저",
    "3665": "고속 케이블·와이어하니스, 서버 인터커넥트",
    # --- 패시브 부품 ---
    "2327": "MLCC·칩저항 세계 선두 패시브 부품",
    "2492": "MLCC·칩저항 패시브 부품",
    "3026": "MLCC 제조 및 전자부품 유통 겸업",
    # --- 리드프레임 ---
    "2351": "반도체 리드프레임 제조",
    "6548": "반도체 리드프레임 제조 (8070 장화는 소재 유통, 6548은 제조 분할)",
    # --- BMC/서버IP/ASIC ---
    "5274": "서버 BMC 칩 세계 독점적 1위",
    "3661": "AI ASIC 디자인 서비스, 하이퍼스케일러 커스텀 칩",
    "3443": "ASIC 디자인 서비스(TSMC 계열)",
    "6533": "RISC-V CPU IP 라이선스",
    "4966": "PCIe 리타이머·고속 신호 IC, AI서버 인터커넥트",
    "3035": "ASIC 디자인 서비스·반도체 IP",
    "5269": "USB·PCIe 고속 I/O 컨트롤러",
    # --- 테스트/프루브/소켓 ---
    "6515": "반도체 테스트 소켓·번인 소켓",
    "6223": "프로브카드·웨이퍼 검사 장비",
    "6510": "프로브카드 전문",
    "2449": "웨이퍼·파이널 테스트 전문",
    "7769": "반도체 테스트 핸들러. 혼하이(2317)와 무관한 별개 회사",
    "2360": "ATE(자동 테스트 장비)·전력 계측",
    # --- 광통신 ---
    "4979": "광통신 트랜시버·레이저 소자",
    "3234": "광통신 레이저 다이오드·모듈",
    "6442": "광통신 커넥터·트랜시버",
    "3081": "광통신용 에피웨이퍼·레이저 칩",
    "3163": "광섬유 수동부품(WDM 필터 등)",
    "3450": "광통신용 레이저 다이오드",
    # --- 화합물반도체(GaAs) ---
    "3105": "GaAs 파운드리 세계 1위, RF·VCSEL 위탁생산",
    "4991": "GaAs·InP 화합물반도체 파운드리",
    "8086": "GaAs 파운드리, RF 프론트엔드",
    # --- 장비/공정소재 ---
    "3680": "EUV 포토마스크 파드·웨이퍼 캐리어",
    "3131": "웨이퍼 세정·습식공정 장비",
    "3583": "반도체 습식장비·재생 웨이퍼",
    "6187": "반도체 패키징·수동부품 조립 자동화 장비",
    "5483": "실리콘 웨이퍼·태양광 소재, 글로벌웨이퍼스(6488) 모회사",
    "6488": "실리콘 웨이퍼 세계 3위",
    "6182": "실리콘 웨이퍼·에피웨이퍼",
    "8021": "PCB 드릴비트",
    "3413": "반도체 공정장비 제조(Applied Materials 협력)",
    "1785": "스퍼터링 타겟·귀금속 공정소재",
    # --- 팹리스/IC설계 ---
    "2454": "모바일 AP 세계 선두 팹리스, AI ASIC 진출",
    "2379": "네트워크·오디오 컨트롤러 팹리스",
    "3034": "디스플레이 드라이버IC·TDDI 세계 선두",
    "3529": "임베디드 비휘발성 메모리 IP 라이선스",
    "6526": "무선 오디오·네트워크 SoC(미디어텍 계열)",
    # --- 전자유통 ---
    "3702": "아시아 최대 반도체 유통",
    "3036": "반도체 유통 2위, Future Electronics 인수",
    "2347": "IT 제품 유통·물류",
    "8112": "메모리 중심 반도체 유통",
    "8096": "반도체 유통·디자인 서비스",
}

# 히트맵 구분 행. GROUPS의 모든 그룹이 정확히 한 번씩 나와야 한다.
STAGES: dict[str, list[str]] = {
    "전공정": ["AI 파운드리", "Mature 파운드리", "OSAT/패키징",
             "메모리", "메모리 모듈/스토리지"],
    "시스템 조립": ["서버 ODM", "후발 ODM", "네트워크/스위치"],
    "기판·소재": ["ABF 기판", "CCL·소재", "서버/AI PCB"],
    "전력·열": ["파워 (PSU/VRM)", "쿨링 (액냉/공랭)"],
    "기구·부품": ["커넥터/레일/섀시", "패시브 부품", "리드프레임"],
    "칩·검사": ["BMC/서버IP/ASIC", "테스트/프루브/소켓"],
    "광·화합물": ["광통신", "화합물반도체(GaAs)"],
    "장비·유통": ["장비/공정소재", "팹리스/IC설계", "전자유통"],
}

FALLBACK = "{ind} 업종 (설명 미등록)"


def all_codes() -> list[str]:
    """Every code in GROUPS, group order preserved, de-duplicated."""
    seen, out = set(), []
    for codes in GROUPS.values():
        for c in codes:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def group_of(code: str) -> str | None:
    for g, codes in GROUPS.items():
        if code in codes:
            return g
    return None


def biz(code: str, industry_kr: str = "") -> str:
    """One-line business description, or the fallback when none is registered."""
    t = (BIZ.get(code) or "").strip()
    return t or FALLBACK.format(ind=industry_kr or "기타")


def heatmap_members(group: str) -> list[str]:
    """Group members for the revenue-sum heatmap, with consolidated subsidiaries
    dropped so a parent's revenue is not counted twice. A subsidiary whose parent
    sits in a different group is kept -- there is nothing to double count."""
    codes = GROUPS.get(group, [])
    inside = set(codes)
    return [c for c in codes if CONSOL_PARENT.get(c) not in inside]


def benchmark_members() -> list[str]:
    """All BoM codes for the whole-universe benchmark row. Here the parent does
    not have to share a group -- if it is anywhere in the 113, its subsidiary
    would be double counted."""
    codes = all_codes()
    inside = set(codes)
    return [c for c in codes if CONSOL_PARENT.get(c) not in inside]


def stage_of(group: str) -> str | None:
    for s, gs in STAGES.items():
        if group in gs:
            return s
    return None


def check_stages() -> list[str]:
    """Problems with the STAGES <-> GROUPS mapping, empty when consistent."""
    listed = [g for gs in STAGES.values() for g in gs]
    problems = []
    for g in GROUPS:
        n = listed.count(g)
        if n == 0:
            problems.append(f"{g}: STAGES에 없음")
        elif n > 1:
            problems.append(f"{g}: STAGES에 {n}번 중복")
    for g in listed:
        if g not in GROUPS:
            problems.append(f"{g}: STAGES에만 있고 GROUPS에 없음")
    return problems


if __name__ == "__main__":
    codes = all_codes()
    dup = [c for c in set(codes) if sum(v.count(c) for v in GROUPS.values()) > 1]
    filled = sum(1 for c in codes if (BIZ.get(c) or "").strip())
    print(f"groups      : {len(GROUPS)}")
    print(f"codes       : {len(codes)}  (unique)")
    print(f"BIZ filled  : {filled}/{len(codes)}")
    print(f"BIZ blank   : {[c for c in codes if not (BIZ.get(c) or '').strip()] or 'none'}")
    print(f"BIZ orphans : {[c for c in BIZ if c not in set(codes)] or 'none'}")
    print(f"multi-group : {dup or 'none'}")
    print(f"stages      : {len(STAGES)}  mapping problems: "
          f"{check_stages() or 'none'}")
    print(f"benchmark   : {len(benchmark_members())} of {len(codes)} codes "
          f"(모자 제외 {len(codes) - len(benchmark_members())})")
    print(f"\nconsolidation exclusions (active ones only):")
    for g in GROUPS:
        dropped = [c for c in GROUPS[g] if c not in heatmap_members(g)]
        if dropped:
            print(f"  {g}: drop {dropped} (parent in same group)")
    print()
    for g, cs in GROUPS.items():
        hm = heatmap_members(g)
        note = f"  heatmap {len(hm)}" if len(hm) != len(cs) else ""
        print(f"  {g:<22} {len(cs):>2}  {' '.join(cs)}{note}")
