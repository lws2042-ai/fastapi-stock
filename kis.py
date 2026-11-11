"""한국투자증권 KIS Developers Open API 클라이언트."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta
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
# Vercel 등 서버리스 환경은 /tmp 만 쓸 수 있다.
DATA_DIR = Path("/tmp") if os.getenv("VERCEL") else Path(__file__).parent
TOKEN_FILE = DATA_DIR / ".kis_token.json"


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


# 초당 요청 수 제한 (실전 20건, 모의 2건이지만 여유 있게 잡는다. .env의 KIS_RPS로 조정 가능)
RPS = float(os.getenv("KIS_RPS", "2" if BASE_URL == MOCK_URL else "5"))
_rate_lock = threading.Lock()
_last_call = 0.0


def _throttle() -> None:
    """요청 사이에 최소 간격(1/RPS초)을 둔다."""
    global _last_call
    with _rate_lock:
        wait = _last_call + 1 / RPS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _get(path: str, tr_id: str, params: dict, retries: int = 5) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    for attempt in range(retries + 1):
        _throttle()
        res = httpx.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=10)
        data = res.json()
        if data.get("msg_cd") == "EGW00201" and attempt < retries:  # 초당 거래건수 초과
            time.sleep(0.4 * (attempt + 1))
            continue
        break
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


def domestic_chart(code: str, period: str = "D", count: int = 180) -> list[dict]:
    """기간별 시세(캔들). 한 번에 100건까지라 날짜를 거슬러 가며 count개를 모은다.

    period: D(일) / W(주) / M(월) / Y(년). 결과는 오래된 순.
    """
    by_date: dict[str, dict] = {}
    end = datetime.now().strftime("%Y%m%d")
    for _ in range(10):  # 안전장치
        data = _get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": "19800101",
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",  # 0 수정주가
            },
        )
        batch = [r for r in data.get("output2", []) if r.get("stck_bsop_date")]
        if not batch:
            break
        for r in batch:
            by_date[r["stck_bsop_date"]] = {
                "date": r["stck_bsop_date"],
                "open": int(r["stck_oprc"]),
                "high": int(r["stck_hgpr"]),
                "low": int(r["stck_lwpr"]),
                "close": int(r["stck_clpr"]),
                "volume": int(r["acml_vol"]),
            }
        if len(by_date) >= count:
            break
        oldest = min(r["stck_bsop_date"] for r in batch)
        end = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    candles = [by_date[d] for d in sorted(by_date)]
    return candles[-count:]


MINUTE_CACHE = DATA_DIR / ".cache" / "minute"


def _minute_bars_of_day(code: str, date: str) -> list[dict]:
    """하루치 1분봉 (오래된 순). 지난 날짜는 바뀌지 않으므로 파일에 캐시한다."""
    cache = MINUTE_CACHE / f"{code}_{date}.json"
    is_today = date == datetime.now().strftime("%Y%m%d")
    if not is_today and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    bars: dict[str, dict] = {}
    hour = "153000"
    for _ in range(6):  # 하루 390분 / 120건 = 4회면 충분
        data = _get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
            "FHKST03010230",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": hour,
                "FID_INPUT_DATE_1": date,
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_FAKE_TICK_INCU_YN": "",
            },
        )
        rows = [r for r in data.get("output2", []) if r.get("stck_bsop_date") == date]
        for r in rows:
            if int(r["stck_prpr"] or 0) > 0:
                bars[r["stck_cntg_hour"]] = {
                    "time": r["stck_cntg_hour"],
                    "open": int(r["stck_oprc"]),
                    "high": int(r["stck_hgpr"]),
                    "low": int(r["stck_lwpr"]),
                    "close": int(r["stck_prpr"]),
                    "volume": int(r["cntg_vol"]),
                }
        if not rows:
            break
        earliest = min(r["stck_cntg_hour"] for r in rows)
        if earliest <= "090100":
            break
        t = datetime.strptime(earliest, "%H%M%S") - timedelta(minutes=1)
        hour = t.strftime("%H%M%S")

    result = [bars[t] for t in sorted(bars)]
    if not is_today and result:
        MINUTE_CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result


def domestic_hourly(code: str, days: int = 12, minutes: int = 60) -> list[dict]:
    """최근 days 거래일의 1분봉을 minutes분봉으로 묶는다. date는 'YYYYMMDDHHMM'(봉 시작 시각)."""
    trading_days = [c["date"] for c in domestic_chart(code, "D", days)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        days_bars = list(ex.map(lambda d: _minute_bars_of_day(code, d), trading_days))
    candles = []
    for date, bars in zip(trading_days, days_bars):
        buckets: dict[int, dict] = {}
        for b in bars:
            # 분봉 시각은 봉이 끝나는 시각 (090100 = 09:00~09:01)
            hh, mm = int(b["time"][:2]), int(b["time"][2:4])
            idx = max(0, hh * 60 + mm - 1 - 9 * 60) // minutes
            c = buckets.get(idx)
            if c is None:
                start = 9 * 60 + idx * minutes
                buckets[idx] = {
                    "date": f"{date}{start // 60:02d}{start % 60:02d}",
                    "open": b["open"], "high": b["high"], "low": b["low"],
                    "close": b["close"], "volume": b["volume"],
                }
            else:
                c["high"] = max(c["high"], b["high"])
                c["low"] = min(c["low"], b["low"])
                c["close"] = b["close"]
                c["volume"] += b["volume"]
        candles.extend(buckets[i] for i in sorted(buckets))
    return candles


def domestic_orderbook(code: str) -> dict:
    """주식현재가 호가/예상체결"""
    return _get(
        "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        "FHKST01010200",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )


# ---------- 국내 순위 (실전투자 전용, 모의투자 미지원) ----------

MARKETS = {"all": "0000", "kospi": "0001", "kosdaq": "1001"}


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
                "trade_value": int(r["acml_tr_pbmn"]) if r.get("acml_tr_pbmn") else None,  # 원
                "market_cap": int(r["stck_avls"]) if r.get("stck_avls") else None,  # 억원
            }
        )
    return result


def rank_market_cap(limit: int = 10, market: str = "kospi") -> list[dict]:
    """시가총액 상위 (보통주)"""
    data = _get(
        "/uapi/domestic-stock/v1/ranking/market-cap",
        "FHPST01740000",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20174",
            "fid_div_cls_code": "1",  # 1 보통주 (우선주 제외)
            "fid_input_iscd": MARKETS[market],
            "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
        },
    )
    return _normalize_rank(data["output"], limit)


def rank_gainers(limit: int = 10, market: str = "kospi") -> list[dict]:
    """등락률 상승 순위 (실시간)"""
    data = _get(
        "/uapi/domestic-stock/v1/ranking/fluctuation",
        "FHPST01700000",
        {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code": "20170",
            "fid_input_iscd": MARKETS[market],
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


def _volume_rank(blng: str, limit: int, market: str) -> list[dict]:
    data = _get(
        "/uapi/domestic-stock/v1/quotations/volume-rank",
        "FHPST01710000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": MARKETS[market],
            "FID_DIV_CLS_CODE": "1",  # 1 보통주
            "FID_BLNG_CLS_CODE": blng,
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000000000",
            "FID_INPUT_PRICE_1": "",
            "FID_INPUT_PRICE_2": "",
            "FID_VOL_CNT": "",
            "FID_INPUT_DATE_1": "",
        },
    )
    return _normalize_rank(data["output"], limit)


def rank_volume(limit: int = 10, market: str = "kospi") -> list[dict]:
    """거래량 순위"""
    return _volume_rank("0", limit, market)


def rank_trade_value(limit: int = 10, market: str = "kospi") -> list[dict]:
    """거래대금 순위"""
    return _volume_rank("3", limit, market)


def rank_active(limit: int = 10, market: str = "kospi") -> list[dict]:
    """거래대금 + 거래량 종합 순위.

    거래량 상위 30과 거래대금 상위 30을 합친 뒤, 각 지표를 후보 중 최댓값으로 나눈 값의 합(0~2)으로 정렬한다.
    거래량만 많은 저가주나 거래대금만 큰 고가주보다 '돈과 손이 함께 몰린' 종목이 위로 온다.
    """
    pool: dict[str, dict] = {}
    for r in _volume_rank("0", 30, market) + _volume_rank("3", 30, market):
        pool[r["code"]] = r
    rows = list(pool.values())
    max_vol = max((r["volume"] for r in rows), default=0) or 1
    max_tv = max((r["trade_value"] or 0 for r in rows), default=0) or 1
    for r in rows:
        r["score"] = round(r["volume"] / max_vol + (r["trade_value"] or 0) / max_tv, 4)
    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows[:limit], 1):
        r["rank"] = i
    return rows[:limit]


# ---------- 해외 주식 ----------

def overseas_price(exchange: str, symbol: str) -> dict:
    """해외주식 현재체결가. exchange: NAS(나스닥) / NYS(뉴욕) / AMS(아멕스) / HKS / TSE 등"""
    return _get(
        "/uapi/overseas-price/v1/quotations/price",
        "HHDFS00000300",
        {"AUTH": "", "EXCD": exchange, "SYMB": symbol},
    )
