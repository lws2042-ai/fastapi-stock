"""국내 주식 주문 / 잔고 조회 (KIS).

⚠️ 실전 키로 KIS_ORDER_ENABLED=true 이면 실제 돈으로 주문이 나간다.

안전장치
- KIS_ORDER_ENABLED=true 가 아니면 모의 실행(dry run): 검증·기록만 하고 KIS로 보내지 않는다.
- 1회 주문 금액이 KIS_MAX_ORDER_AMOUNT(기본 100만 원)를 넘으면 거부.
- 같은 주문이 짧은 시간(5초) 안에 다시 오면 거부 (버튼 연타 방지).
- 모든 주문 요청과 결과를 orders.jsonl 에 기록.
- 주문 전송 중 네트워크 오류는 재시도하지 않는다 (중복 체결 위험) → 잔고/체결 내역으로 확인.
"""

import json
import os
import threading
import time
from datetime import datetime

import httpx

import kis

ACCOUNT = os.getenv("KIS_ACCOUNT", "")  # 예: 12345678-01 (종합계좌번호 8자리 - 상품코드 2자리)
ORDER_ENABLED = os.getenv("KIS_ORDER_ENABLED", "false").lower() == "true"
MAX_ORDER_AMOUNT = int(os.getenv("KIS_MAX_ORDER_AMOUNT", "1000000"))
IS_MOCK = kis.BASE_URL == kis.MOCK_URL

ORDER_LOG = kis.DATA_DIR / ".cache" / "orders.jsonl"
DUP_WINDOW = 5.0  # 초

# 주식주문(현금) / 주식잔고조회 TR (실전 / 모의)
TR_BUY = "VTTC0012U" if IS_MOCK else "TTTC0012U"
TR_SELL = "VTTC0011U" if IS_MOCK else "TTTC0011U"
TR_BALANCE = "VTTC8434R" if IS_MOCK else "TTTC8434R"

_lock = threading.Lock()
_recent: dict[tuple, float] = {}


def _account() -> tuple[str, str]:
    parts = ACCOUNT.replace(" ", "").split("-")
    if len(parts) != 2 or len(parts[0]) != 8 or len(parts[1]) != 2 or not (parts[0] + parts[1]).isdigit():
        raise kis.KISError(".env에 KIS_ACCOUNT=계좌번호8자리-상품코드2자리 (예: 12345678-01) 를 설정하세요.")
    return parts[0], parts[1]


def config() -> dict:
    return {
        "enabled": ORDER_ENABLED,
        "mock": IS_MOCK,
        "max_order_amount": MAX_ORDER_AMOUNT,
        "account_set": bool(ACCOUNT),
        "account": (ACCOUNT[:4] + "****" + ACCOUNT[8:]) if ACCOUNT else None,
    }


def _log(entry: dict) -> None:
    ORDER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ORDER_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def order_log(limit: int = 50) -> list[dict]:
    if not ORDER_LOG.exists():
        return []
    lines = ORDER_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines)]


def _post(path: str, tr_id: str, body: dict) -> dict:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {kis.get_access_token()}",
        "appkey": kis.APP_KEY,
        "appsecret": kis.APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    for attempt in range(3):
        kis._throttle()
        # 타임아웃 등 네트워크 오류는 그대로 올린다 (주문이 들어갔는지 알 수 없으므로 재시도 금지)
        res = httpx.post(f"{kis.BASE_URL}{path}", headers=headers, json=body, timeout=10)
        data = res.json()
        if data.get("msg_cd") == "EGW00201" and attempt < 2:  # 초당 건수 초과 = 접수 전 거부라 재시도 안전
            time.sleep(0.5 * (attempt + 1))
            continue
        return data
    return data


def place_order(code: str, side: str, qty: int, price: int | None) -> dict:
    """side: buy/sell, price=None 이면 시장가."""
    if side not in ("buy", "sell"):
        raise ValueError("side는 buy 또는 sell")
    if not (len(code) == 6 and code.isalnum()):
        raise ValueError("종목코드는 6자리")
    if qty <= 0:
        raise ValueError("수량은 1 이상")
    if price is not None and price <= 0:
        raise ValueError("지정가는 0보다 커야 합니다")

    # 금액 상한: 시장가는 현재가로 추정
    ref_price = price or int(kis.domestic_price(code)["output"]["stck_prpr"])
    amount = ref_price * qty
    if amount > MAX_ORDER_AMOUNT:
        raise ValueError(f"주문 금액 {amount:,}원이 1회 한도 {MAX_ORDER_AMOUNT:,}원을 넘습니다 (.env KIS_MAX_ORDER_AMOUNT)")

    key = (code, side, qty, price)
    now = time.monotonic()
    with _lock:
        if now - _recent.get(key, 0) < DUP_WINDOW:
            raise ValueError("같은 주문이 방금 들어왔습니다. 잠시 후 다시 시도하세요.")
        _recent[key] = now

    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "type": "limit" if price else "market",
        "est_amount": amount,
        "mock_account": IS_MOCK,
    }

    if not ORDER_ENABLED:
        entry.update(status="dry_run", message="모의 실행: 실제 주문은 전송되지 않았습니다 (KIS_ORDER_ENABLED=false)")
        _log(entry)
        return entry

    cano, prdt = _account()
    body = {
        "CANO": cano,
        "ACNT_PRDT_CD": prdt,
        "PDNO": code,
        "ORD_DVSN": "00" if price else "01",  # 00 지정가, 01 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(price or 0),
    }
    try:
        data = _post("/uapi/domestic-stock/v1/trading/order-cash", TR_BUY if side == "buy" else TR_SELL, body)
    except Exception as e:
        entry.update(status="unknown", message=f"전송 중 오류 — 체결 여부를 잔고에서 확인하세요: {e!r}")
        _log(entry)
        raise kis.KISError(entry["message"]) from e

    if data.get("rt_cd") != "0":
        entry.update(status="rejected", message=f"[{data.get('msg_cd')}] {data.get('msg1')}")
        _log(entry)
        raise kis.KISError(entry["message"])

    out = data.get("output", {})
    entry.update(status="accepted", order_no=out.get("ODNO"), order_time=out.get("ORD_TMD"), message=data.get("msg1"))
    _log(entry)
    return entry


def balance() -> dict:
    """보유 종목 + 계좌 요약 (조회 전용)"""
    cano, prdt = _account()
    data = kis._get(
        "/uapi/domestic-stock/v1/trading/inquire-balance",
        TR_BALANCE,
        {
            "CANO": cano,
            "ACNT_PRDT_CD": prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",  # 종목별
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        },
    )
    n = lambda v: int(float(v or 0))  # noqa: E731
    holdings = [
        {
            "code": r.get("pdno"),
            "name": r.get("prdt_name"),
            "qty": n(r.get("hldg_qty")),
            "orderable_qty": n(r.get("ord_psbl_qty")),
            "avg_price": float(r.get("pchs_avg_pric") or 0),
            "price": n(r.get("prpr")),
            "eval_amount": n(r.get("evlu_amt")),
            "profit": n(r.get("evlu_pfls_amt")),
            "profit_rate": float(r.get("evlu_pfls_rt") or 0),
        }
        for r in data.get("output1", [])
        if n(r.get("hldg_qty")) > 0
    ]
    s = (data.get("output2") or [{}])[0]
    return {
        "holdings": holdings,
        "summary": {
            "cash": n(s.get("dnca_tot_amt")),  # 예수금
            "d2_cash": n(s.get("prvs_rcdl_excc_amt")),  # D+2 예수금
            "stock_eval": n(s.get("scts_evlu_amt")),
            "total_eval": n(s.get("tot_evlu_amt")),
            "profit": n(s.get("evlu_pfls_smtl_amt")),
        },
    }
