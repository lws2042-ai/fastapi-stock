"""한국투자증권 KIS Developers Open API 클라이언트."""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

REAL_URL = "https://openapi.koreainvestment.com:9443"
MOCK_URL = "https://openapivts.koreainvestment.com:29443"

APP_KEY = os.getenv("KIS_APP_KEY", "")
APP_SECRET = os.getenv("KIS_APP_SECRET", "")
BASE_URL = MOCK_URL if os.getenv("KIS_MOCK", "false").lower() == "true" else REAL_URL

# 토큰은 24시간 유효하고 발급은 1분에 1회로 제한되므로 파일에 캐시한다.
TOKEN_FILE = Path(__file__).parent / ".kis_token.json"


class KISError(Exception):
    pass


def _load_cached_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expires = datetime.strptime(data["expires"], "%Y-%m-%d %H:%M:%S")
    if data.get("base_url") == BASE_URL and (expires - datetime.now()).total_seconds() > 600:
        return data["access_token"]
    return None


_token_lock = threading.Lock()


def get_access_token() -> str:
    if not APP_KEY or not APP_SECRET:
        raise KISError(".env 파일에 KIS_APP_KEY, KIS_APP_SECRET을 설정하세요.")

    # 여러 요청이 동시에 들어와도 토큰은 한 번만 발급되도록 잠근다.
    with _token_lock:
        return _load_cached_token() or _issue_token()


def _issue_token() -> str:
    res = httpx.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    data = res.json()
    if "access_token" not in data:
        raise KISError(f"토큰 발급 실패: {data}")

    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": data["access_token"],
                "expires": data["access_token_token_expired"],
                "base_url": BASE_URL,
            }
        ),
        encoding="utf-8",
    )
    return data["access_token"]


def _get(path: str, tr_id: str, params: dict) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    res = httpx.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
    data = res.json()
    if data.get("rt_cd") != "0":
        raise KISError(f"[{data.get('msg_cd')}] {data.get('msg1', data)}")
    return data


# ---------- 국내 주식 ----------

def domestic_price(code: str) -> dict:
    """주식현재가 시세 (예: 005930 삼성전자)"""
    return _get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )


def domestic_daily(code: str, period: str = "D") -> dict:
    """주식현재가 일자별 (최근 30건). period: D(일) / W(주) / M(월)"""
    return _get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
        "FHKST01010400",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "1",  # 수정주가 반영
        },
    )


def domestic_orderbook(code: str) -> dict:
    """주식현재가 호가/예상체결"""
    return _get(
        "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "FHKST01010200",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )


# ---------- 국내 순위 (실전투자 전용, 모의투자 미지원) ----------

def _normalize_rank(rows: list[dict], limit: int) -> list[dict]:
    """순위 API마다 다른 필드명을 같은 형식으로 맞춘다."""
    result = []
    for r in rows[:limit]:
        result.append(
            {
                "rank": int(r.get("data_rank") or len(result) + 1),
                "code": r.get("mksc_shrn_iscd") or r.get("stck_shrn_iscd"),
                "name": r.get("hts_kor_isnm"),
                "price": int(r.get("stck_prpr") or 0),
                "change": int(r.get("prdy_vrss") or 0),
                "change_rate": float(r.get("prdy_ctrt") or 0),
                "sign": r.get("prdy_vrss_sign"),
                "volume": int(r.get("acml_vol") or 0),
                "market_cap": int(r["stck_avls"]) if r.get("stck_avls") else None,  # 억원
            }
        )
    return result


def rank_market_cap(limit: int = 10) -> list[dict]:
    """시가총액 상위"""
    data = _get(
        "/uapi/domestic-stock/v1/ranking/market-cap",
        "FHPST01740000",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20174",
            "fid_div_cls_code": "0",  # 전체
            "fid_input_iscd": "0000",  # 전체 (0001 코스피, 1001 코스닥)
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
        },
    )
    return _normalize_rank(data["output"], limit)


def rank_gainers(limit: int = 10) -> list[dict]:
    """등락률 상승 순위"""
    data = _get(
        "/uapi/domestic-stock/v1/ranking/fluctuation",
        "FHPST01700000",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": "0000",
            "fid_rank_sort_cls_code": "0",  # 0 상승율순
            "fid_input_cnt_1": "0",
            "fid_prc_cls_code": "1",  # 1 전일종가 대비
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_div_cls_code": "0",
            "fid_rsfl_rate1": "",
            "fid_rsfl_rate2": "",
        },
    )
    return _normalize_rank(data["output"], limit)


def rank_volume(limit: int = 10) -> list[dict]:
    """거래량 순위"""
    data = _get(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",  # 0 평균거래량
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        },
    )
    return _normalize_rank(data["output"], limit)


# ---------- 해외 주식 ----------

def overseas_price(exchange: str, symbol: str) -> dict:
    """해외주식 현재체결가. exchange: NAS(나스닥) / NYS(뉴욕) / AMS(아멕스) / HKS / TSE 등"""
    return _get(
        "/uapi/overseas-price/v1/quotations/price",
        "HHDFS00000300",
        {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
    )
