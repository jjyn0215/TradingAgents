"""
한국투자증권 REST API 클라이언트

- KISClient: 한국투자증권 Open API (잔고, 매수, 매도, 현재가, 시가총액 순위)
- format_krw(): 원화 포맷 유틸리티
"""

import os
import logging
import datetime
import requests

logger = logging.getLogger(__name__)


class KISClient:
    """한국투자증권 Open API 래퍼 (REST 직접 호출)"""

    REAL_URL = "https://openapi.koreainvestment.com:9443"
    VIRTUAL_URL = "https://openapivts.koreainvestment.com:29443"

    def __init__(self):
        self.app_key = os.getenv("KIS_APP_KEY", "")
        self.app_secret = os.getenv("KIS_APP_SECRET", "")
        self.account_no = os.getenv("KIS_ACCOUNT_NO", "")
        self.virtual = os.getenv("KIS_VIRTUAL", "true").lower() == "true"
        self.max_order_amount = int(os.getenv("KIS_MAX_ORDER_AMOUNT", "1000000"))

        self.base_url = self.VIRTUAL_URL if self.virtual else self.REAL_URL
        self._token: str | None = None
        self._token_expires: datetime.datetime | None = None

        # 계좌번호 파싱 (12345678-01 → cano=12345678, acnt_prdt_cd=01)
        parts = self.account_no.split("-") if self.account_no else []
        self.cano = parts[0] if parts else ""
        self.acnt_prdt_cd = parts[1] if len(parts) > 1 else "01"

    @property
    def is_configured(self) -> bool:
        """KIS API 인증 정보가 설정되었는지 확인"""
        return bool(self.app_key and self.app_secret and self.account_no)

    # ── 인증 ─────────────────────────────────────────────────

    def _ensure_token(self):
        """Access token이 없거나 만료됐으면 재발급"""
        if (
            self._token
            and self._token_expires
            and datetime.datetime.now() < self._token_expires
        ):
            return
        self._issue_token()

    def _issue_token(self):
        """OAuth2 access token 발급"""
        resp = requests.post(
            f"{self.base_url}/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = datetime.datetime.strptime(
            data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S"
        )

    def _headers(self, tr_id: str) -> dict:
        """공통 API 헤더 생성"""
        self._ensure_token()
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

    # ── 잔고 조회 ────────────────────────────────────────────

    def get_balance(self) -> dict:
        """주식 잔고 + 계좌 요약 조회"""
        tr_id = "VTTC8434R" if self.virtual else "TTTC8434R"
        resp = requests.get(
            f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._headers(tr_id),
            params={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        holdings = []
        for item in data.get("output1", []):
            if int(item.get("hldg_qty", "0")) > 0:
                holdings.append({
                    "ticker": item.get("pdno", ""),
                    "name": item.get("prdt_name", ""),
                    "qty": int(item.get("hldg_qty", "0")),
                    "avg_price": int(float(item.get("pchs_avg_pric", "0"))),
                    "current_price": int(item.get("prpr", "0")),
                    "pnl": int(item.get("evlu_pfls_amt", "0")),
                    "pnl_rate": float(item.get("evlu_pfls_rt", "0")),
                })

        summary = {}
        output2 = data.get("output2", [])
        if output2:
            s = output2[0] if isinstance(output2, list) else output2
            summary = {
                "total_eval": int(s.get("tot_evlu_amt", "0")),
                "total_pnl": int(s.get("evlu_pfls_smtl_amt", "0")),
                "cash": int(float(s.get("dnca_tot_amt", "0"))),
            }

        return {"holdings": holdings, "summary": summary}

    # ── 현재가 조회 ──────────────────────────────────────────

    def get_price(self, ticker: str) -> int:
        """종목 현재가 조회"""
        resp = requests.get(
            f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers("FHKST01010100"),
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return int(data.get("output", {}).get("stck_prpr", "0"))

    # ── 매수 ─────────────────────────────────────────────────

    def buy_stock(self, ticker: str, qty: int, price: int = 0) -> dict:
        """주식 매수 (price=0 → 시장가)"""
        tr_id = "VTTC0802U" if self.virtual else "TTTC0802U"
        resp = requests.post(
            f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            json={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "01",  # 시장가
                "ORD_QTY": str(qty),
                "ORD_UNPR": str(price) if price else "0",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": data.get("rt_cd") == "0",
            "message": data.get("msg1", ""),
            "order_no": data.get("output", {}).get("ODNO", ""),
        }

    # ── 매도 ─────────────────────────────────────────────────

    def sell_stock(self, ticker: str, qty: int, price: int = 0) -> dict:
        """주식 매도 (price=0 → 시장가)"""
        tr_id = "VTTC0801U" if self.virtual else "TTTC0801U"
        resp = requests.post(
            f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_id),
            json={
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "PDNO": ticker,
                "ORD_DVSN": "01",  # 시장가
                "ORD_QTY": str(qty),
                "ORD_UNPR": str(price) if price else "0",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": data.get("rt_cd") == "0",
            "message": data.get("msg1", ""),
            "order_no": data.get("output", {}).get("ODNO", ""),
        }

    # ── 휴장일 조회 ──────────────────────────────────────────────

    _holiday_cache: dict[str, bool] = {}  # {"YYYYMMDD": True/False}

    def is_market_open(self, dt: datetime.date | None = None) -> bool:
        """KIS 국내휴장일조회 API로 개장일 여부 반환.

        [국내주식] 업종/기타 > 국내휴장일조회 [국내주식-040]
        - URL: /uapi/domestic-stock/v1/quotations/chk-holiday
        - TR_ID: CTCA0903R (모의/실전 공통)
        - 응답 opnd_yn == "Y" → 개장일
        - 1일 1회 호출 권장이므로 결과를 캐시합니다.
        """
        if dt is None:
            dt = datetime.date.today()
        key = dt.strftime("%Y%m%d")

        # 주말은 API 호출 없이 바로 False
        if dt.weekday() >= 5:
            return False

        # 캐시 히트
        if key in self._holiday_cache:
            return self._holiday_cache[key]

        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/chk-holiday",
                headers=self._headers("CTCA0903R"),
                params={"BASS_DT": key, "CTX_AREA_FK": "", "CTX_AREA_NK": ""},
            )
            resp.raise_for_status()
            items = resp.json().get("output", [])
            # 응답은 기준일 이후 날짜 목록 — 해당 날짜 찾기
            for item in items:
                if item.get("bass_dt") == key:
                    is_open = item.get("opnd_yn", "N") == "Y"
                    self._holiday_cache[key] = is_open
                    return is_open
            # 해당 날짜를 못 찾은 경우 (평일이면 개장으로 간주)
            self._holiday_cache[key] = True
            return True
        except Exception as e:
            logger.warning("KIS 휴장일 조회 실패 (개장으로 간주): %s", e)
            return True

    # ── 시가총액 순위 조회 ──────────────────────────────────────

    def get_top_market_cap(self, count: int = 5) -> list[dict]:
        """KIS 공식 REST API로 코스피 시가총액 상위 종목 조회

        [국내주식] 순위분석 > 국내주식 시가총액 상위 [v1_국내주식-091]
        - URL: /uapi/domestic-stock/v1/ranking/market-cap
        - TR_ID: FHPST01740000
        """
        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/ranking/market-cap",
                headers=self._headers("FHPST01740000"),
                params={
                    "fid_cond_mrkt_div_code": "J",       # KRX (거래소)
                    "fid_cond_scr_div_code": "20174",     # Unique key
                    "fid_input_iscd": "0001",             # 0001: 거래소(코스피)
                    "fid_div_cls_code": "1",              # 1: 보통주만
                    "fid_trgt_cls_code": "0",             # 전체
                    "fid_trgt_exls_cls_code": "0",        # 전체
                    "fid_input_price_1": "",               # 가격 필터 없음
                    "fid_input_price_2": "",               # 가격 필터 없음
                    "fid_vol_cnt": "",                     # 거래량 필터 없음
                },
            )
            resp.raise_for_status()
            data = resp.json()

            items = data.get("output", [])
            results = []
            for item in items[:count]:
                ticker = item.get("mksc_shrn_iscd", "")
                name = item.get("hts_kor_isnm", "").strip()
                price = int(item.get("stck_prpr", "0"))
                market_cap = int(item.get("stck_avls", "0")) * 1_0000_0000  # 억 → 원
                volume = int(item.get("acml_vol", "0"))
                rank = int(item.get("data_rank", str(len(results) + 1)))

                if ticker:
                    results.append({
                        "rank": rank,
                        "ticker": ticker,
                        "name": name,
                        "price": price,
                        "market_cap": market_cap,
                        "volume": volume,
                    })
            return results

        except Exception as e:
            logger.error("KIS 시가총액 순위 조회 실패: %s", e)
            return []

    # ── 거래량 순위 조회 ──────────────────────────────────────

    def get_volume_rank(self, count: int = 30) -> list[dict]:
        """거래량 상위 종목 조회

        [국내주식] 순위분석 > 거래량순위 [v1_국내주식-047]
        - URL: /uapi/domestic-stock/v1/quotations/volume-rank
        - TR_ID: FHPST01710000
        """
        import time
        time.sleep(0.2)
        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers=self._headers("FHPST01710000"),
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_COND_SCR_DIV_CODE": "20171",
                    "FID_INPUT_ISCD": "0001",             # 코스피
                    "FID_DIV_CLS_CODE": "1",              # 보통주
                    "FID_BLNG_CLS_CODE": "0",             # 평균거래량순
                    "FID_TRGT_CLS_CODE": "111111111",
                    "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                    "FID_INPUT_PRICE_1": "",
                    "FID_INPUT_PRICE_2": "",
                    "FID_VOL_CNT": "",
                    "FID_INPUT_DATE_1": "",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("output", [])
            results = []
            for item in items[:count]:
                ticker = item.get("mksc_shrn_iscd", "")
                if not ticker:
                    continue
                results.append({
                    "ticker": ticker,
                    "name": item.get("hts_kor_isnm", "").strip(),
                    "rank": int(item.get("data_rank", "0")),
                    "price": int(item.get("stck_prpr", "0")),
                    "prdy_ctrt": float(item.get("prdy_ctrt", "0")),
                    "acml_vol": int(item.get("acml_vol", "0")),
                    "vol_inrt": float(item.get("vol_inrt", "0")),
                })
            return results
        except Exception as e:
            logger.error("거래량 순위 조회 실패: %s", e)
            return []

    # ── 체결강도 상위 조회 ────────────────────────────────────

    def get_volume_power(self, count: int = 30) -> list[dict]:
        """체결강도 상위 종목 조회

        [국내주식] 순위분석 > 국내주식 체결강도 상위 [v1_국내주식-101]
        - URL: /uapi/domestic-stock/v1/ranking/volume-power
        - TR_ID: FHPST01680000
        - 핵심: tday_rltv (당일 체결강도) — 100 이상이면 매수세 우위
        """
        import time
        time.sleep(0.2)
        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/ranking/volume-power",
                headers=self._headers("FHPST01680000"),
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20168",
                    "fid_input_iscd": "0001",             # 코스피
                    "fid_div_cls_code": "1",              # 보통주
                    "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_input_price_1": "",
                    "fid_input_price_2": "",
                    "fid_vol_cnt": "",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("output", [])
            results = []
            for item in items[:count]:
                ticker = item.get("stck_shrn_iscd", "")
                if not ticker:
                    continue
                results.append({
                    "ticker": ticker,
                    "name": item.get("hts_kor_isnm", "").strip(),
                    "rank": int(item.get("data_rank", "0")),
                    "price": int(item.get("stck_prpr", "0")),
                    "prdy_ctrt": float(item.get("prdy_ctrt", "0")),
                    "tday_rltv": float(item.get("tday_rltv", "0")),
                })
            return results
        except Exception as e:
            logger.error("체결강도 순위 조회 실패: %s", e)
            return []

    # ── 등락률 순위 조회 ──────────────────────────────────────

    def get_fluctuation_rank(self, count: int = 30) -> list[dict]:
        """등락률 상위 종목 조회 (상승률 순)

        [국내주식] 순위분석 > 등락률 순위 [v1_국내주식-088]
        - URL: /uapi/domestic-stock/v1/ranking/fluctuation
        - TR_ID: FHPST01700000
        """
        import time
        time.sleep(0.2)
        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/ranking/fluctuation",
                headers=self._headers("FHPST01700000"),
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20170",
                    "fid_input_iscd": "0000",             # 전체
                    "fid_rank_sort_cls_code": "0",         # 등락률순
                    "fid_input_cnt_1": str(count),
                    "fid_prc_cls_code": "0",
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
            resp.raise_for_status()
            items = resp.json().get("output", [])
            results = []
            for item in items[:count]:
                ticker = item.get("stck_shrn_iscd", "")
                if not ticker:
                    continue
                results.append({
                    "ticker": ticker,
                    "name": item.get("hts_kor_isnm", "").strip(),
                    "rank": int(item.get("data_rank", "0")),
                    "price": int(item.get("stck_prpr", "0")),
                    "prdy_ctrt": float(item.get("prdy_ctrt", "0")),
                    "acml_vol": int(item.get("acml_vol", "0")),
                    "cnnt_ascn_dynu": int(item.get("cnnt_ascn_dynu", "0")),
                })
            return results
        except Exception as e:
            logger.error("등락률 순위 조회 실패: %s", e)
            return []

    # ── 대량체결건수 상위 조회 ────────────────────────────────

    def get_bulk_trans(self, count: int = 30) -> list[dict]:
        """대량체결건수 매수 상위 종목 조회

        [국내주식] 순위분석 > 국내주식 대량체결건수 상위 [국내주식-107]
        - URL: /uapi/domestic-stock/v1/ranking/bulk-trans-num
        - TR_ID: FHKST190900C0
        """
        import time
        time.sleep(0.2)
        try:
            resp = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/ranking/bulk-trans-num",
                headers=self._headers("FHKST190900C0"),
                params={
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "11909",
                    "fid_input_iscd": "0001",             # 코스피
                    "fid_rank_sort_cls_code": "0",         # 매수 상위
                    "fid_div_cls_code": "0",
                    "fid_input_price_1": "",
                    "fid_aply_rang_prc_1": "",
                    "fid_aply_rang_prc_2": "",
                    "fid_input_iscd_2": "",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_trgt_cls_code": "0",
                    "fid_vol_cnt": "",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("output", [])
            results = []
            for item in items[:count]:
                ticker = item.get("mksc_shrn_iscd", "")
                if not ticker:
                    continue
                results.append({
                    "ticker": ticker,
                    "name": item.get("hts_kor_isnm", "").strip(),
                    "rank": int(item.get("data_rank", "0")),
                    "price": int(item.get("stck_prpr", "0")),
                    "prdy_ctrt": float(item.get("prdy_ctrt", "0")),
                    "buy_cnt": int(item.get("shnu_cntg_csnu", "0")),
                    "ntby_cnqn": int(item.get("ntby_cnqn", "0")),
                })
            return results
        except Exception as e:
            logger.error("대량체결 순위 조회 실패: %s", e)
            return []

    # ── 보유종목 전량 매도 ────────────────────────────────────

    def sell_all_holdings(self) -> list[dict]:
        """보유 종목 전량 시장가 매도 후 결과 리스트 반환.

        Returns:
            [{"ticker", "name", "qty", "avg_price", "sell_price", "success", "message", "order_no"}, ...]
        """
        import time
        balance = self.get_balance()
        holdings = balance.get("holdings", [])
        results = []
        for h in holdings:
            ticker = h["ticker"]
            qty = h["qty"]
            if qty <= 0:
                continue
            try:
                sell_result = self.sell_stock(ticker, qty)
                # 매도 직후 현재가 조회
                try:
                    sell_price = self.get_price(ticker)
                except Exception:
                    sell_price = h.get("current_price", 0)
                results.append({
                    "ticker": ticker,
                    "name": h.get("name", ticker),
                    "qty": qty,
                    "avg_price": h.get("avg_price", 0),
                    "sell_price": sell_price,
                    "success": sell_result.get("success", False),
                    "message": sell_result.get("message", ""),
                    "order_no": sell_result.get("order_no", ""),
                })
            except Exception as e:
                results.append({
                    "ticker": ticker,
                    "name": h.get("name", ticker),
                    "qty": qty,
                    "avg_price": h.get("avg_price", 0),
                    "sell_price": 0,
                    "success": False,
                    "message": str(e)[:200],
                    "order_no": "",
                })
            time.sleep(0.3)  # API rate limit 방지
        return results


# ── 유틸리티 ─────────────────────────────────────────────────


def format_krw(amount: int) -> str:
    """숫자를 한국 원화 축약 포맷으로 변환"""
    if abs(amount) >= 1_0000_0000_0000:
        return f"{amount / 1_0000_0000_0000:.1f}조"
    if abs(amount) >= 1_0000_0000:
        return f"{amount / 1_0000_0000:.1f}억"
    if abs(amount) >= 1_0000:
        return f"{amount / 1_0000:.0f}만"
    return f"{amount:,}"
