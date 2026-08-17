# -*- coding: utf-8 -*-
"""
매일 아침 3개 사이트(교보문고 종이책 / 교보문고 eBook / 예스24 전자책)의
일간 베스트셀러 순위를 긁어와서 data.json / history.json 을 만드는 스크립트.

실행 방법:
    pip install -r requirements.txt
    playwright install --with-deps chromium
    python crawl.py

결과물:
    ../data.json     - 오늘자 순위 (index.html이 읽어서 화면에 표시)
    ../history.json  - 날짜별 순위 히스토리 누적 (최근 90일)

※ 사이트 구조가 바뀌면 아래 SELECTORS 부분만 고치면 됩니다.
   (실제로 2026-08-12에 화면 캡처해서 확인한 구조 기준으로 작성됨)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

# ---------- 기본 설정 ----------
KST = timezone(timedelta(hours=9))
TODAY = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_JSON = os.path.join(BASE_DIR, "data.json")
HISTORY_JSON = os.path.join(BASE_DIR, "history.json")

STORES = ["kyobo_paper", "kyobo_ebook", "yes24_ebook"]
STORE_URLS = {
    "kyobo_paper": "https://store.kyobobook.co.kr/bestseller/online/daily/domestic",
    "kyobo_ebook": "https://ebook.kyobobook.co.kr/dig/etc/landing/best?rdng=day",
    "yes24_ebook": "https://www.yes24.com/product/category/daybestseller?CategoryNumber=017",
}
HISTORY_MAX_DAYS = 90


def norm_title(title: str) -> str:
    """서로 다른 서점의 같은 책을 매칭하기 위해 제목을 정규화."""
    t = re.sub(r"[\s\(\)\[\]『』《》〈〉·:：,.\-!?'\"“”‘’]", "", title or "")
    return t.lower()


# ---------- 사이트별 크롤링 함수 ----------
def scrape_kyobo_paper(page):
    """교보문고 종이책 온라인 일간 베스트"""
    page.goto(STORE_URLS["kyobo_paper"], wait_until="networkidle", timeout=60000)
    page.wait_for_selector("a.prod_link.line-clamp-2", timeout=20000)
    items = page.query_selector_all("ol.grid > li")
    results = []
    for idx, li in enumerate(items, start=1):
        title_el = li.query_selector("a.prod_link.line-clamp-2")
        if not title_el:
            continue
        title = title_el.inner_text().strip()
        href = title_el.get_attribute("href") or ""
        pid = href.rstrip("/").split("/")[-1]

        author, pub = "", ""
        try:
            meta_text = title_el.evaluate(
                "el => el.nextElementSibling ? el.nextElementSibling.innerText : ''"
            )
            parts = [p.strip() for p in meta_text.split("·")]
            if len(parts) >= 1:
                author = parts[0]
            if len(parts) >= 2:
                pub = parts[1]
        except Exception:
            pass

        ship = ""
        ship_el = li.query_selector("p.fz-14.mt-2")
        if ship_el:
            ship = re.sub(r"\s+", " ", ship_el.inner_text()).strip()

        results.append(
            {"t": idx, "title": title, "author": author, "pub": pub, "pid": pid, "ship": ship}
        )
    return results


def scrape_kyobo_ebook(page):
    """교보문고 eBook 일간 베스트"""
    page.goto(STORE_URLS["kyobo_ebook"], wait_until="networkidle", timeout=60000)
    page.wait_for_selector("div#prdList div.prodDt", timeout=20000)
    items = page.query_selector_all("div#prdList > div.prodDt")
    results = []
    for li in items:
        rank_el = li.query_selector("em.rank")
        if not rank_el:
            continue
        try:
            rank = int(rank_el.inner_text().strip())
        except ValueError:
            continue

        title_el = li.query_selector("h3 a")
        title = title_el.inner_text().strip() if title_el else ""
        href = title_el.get_attribute("href") if title_el else ""
        pid = href.rstrip("/").split("/")[-1] if href else ""

        info_spans = li.query_selector_all("p.prodDt_info > span")
        texts = [s.inner_text().strip() for s in info_spans]
        author = texts[0] if len(texts) > 0 else ""
        pub = texts[1] if len(texts) > 1 else ""

        results.append(
            {"t": rank, "title": title, "author": author, "pub": pub, "pid": pid, "ship": ""}
        )
    return results


def scrape_yes24_ebook(page):
    """예스24 전자책(eBook) 일간 베스트"""
    page.goto(STORE_URLS["yes24_ebook"], wait_until="networkidle", timeout=60000)
    page.wait_for_selector("ul#yesBestList li", timeout=20000)
    items = page.query_selector_all("ul#yesBestList > li")
    results = []
    for li in items:
        rank_el = li.query_selector("em.ico.rank")
        if not rank_el:
            continue
        try:
            rank = int(rank_el.inner_text().strip())
        except ValueError:
            continue

        title_el = li.query_selector("a.gd_name")
        title = title_el.inner_text().strip() if title_el else ""
        pid = li.get_attribute("data-goods-no") or ""

        author_el = li.query_selector("span.info_auth")
        author = re.sub(r"\s+", " ", author_el.inner_text()).strip() if author_el else ""
        pub_el = li.query_selector("span.info_pub")
        pub = re.sub(r"\s+", " ", pub_el.inner_text()).strip() if pub_el else ""

        results.append(
            {"t": rank, "title": title, "author": author, "pub": pub, "pid": pid, "ship": ""}
        )
    return results


SCRAPERS = {
    "kyobo_paper": scrape_kyobo_paper,
    "kyobo_ebook": scrape_kyobo_ebook,
    "yes24_ebook": scrape_yes24_ebook,
}


# ---------- 병합 / 전일 대비 계산 ----------
def build_books(scraped: dict) -> list:
    """서로 다른 서점 결과를 같은 책끼리 묶는다 (제목 정규화 매칭)."""
    merged = {}
    for store, items in scraped.items():
        for it in items:
            key = norm_title(it["title"])
            if not key:
                continue
            if key not in merged:
                merged[key] = {
                    "isbn": key,
                    "title": it["title"],
                    "author": it["author"],
                    "pub": it["pub"],
                }
            merged[key][store] = {"t": it["t"], "pid": it["pid"], "ship": it.get("ship", "")}
    return list(merged.values())


def load_prev_pid_ranks():
    """어제자 data.json에서 (서점, 상품ID) -> 순위 매핑을 읽어온다."""
    if not os.path.exists(DATA_JSON):
        return {s: {} for s in STORES}, None
    try:
        with open(DATA_JSON, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        return {s: {} for s in STORES}, None

    prev_date = old.get("today")
    pid_ranks = {s: {} for s in STORES}
    for cat in old.get("data", {}).values():
        for b in cat.get("books", []):
            for s in STORES:
                v = b.get(s)
                if v and v.get("pid"):
                    pid_ranks[s][v["pid"]] = v["t"]
    return pid_ranks, prev_date


def apply_prev_ranks(books: list, pid_ranks: dict):
    for b in books:
        for s in STORES:
            v = b.get(s)
            if v:
                v["p"] = pid_ranks.get(s, {}).get(v["pid"])  # 없으면 None(=신규 NEW)


# ---------- 히스토리 누적 ----------
def update_history(output: dict):
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = {"dates": [], "books": {}}
    else:
        hist = {"dates": [], "books": {}}

    if TODAY in hist["dates"]:
        idx = hist["dates"].index(TODAY)
    else:
        hist["dates"].append(TODAY)
        idx = len(hist["dates"]) - 1
        for bk in hist["books"].values():
            for cat_series in bk.get("series", {}).values():
                for arr in cat_series.values():
                    arr.append(None)

    n = len(hist["dates"])
    for b in output["data"]["all"]["books"]:
        key = b["isbn"]
        bk = hist["books"].setdefault(
            key, {"title": b["title"], "author": b["author"], "pub": b["pub"], "series": {}}
        )
        bk["title"], bk["author"], bk["pub"] = b["title"], b["author"], b["pub"]
        cat_series = bk["series"].setdefault("all", {})
        for s in STORES:
            arr = cat_series.setdefault(s, [None] * n)
            while len(arr) < n:
                arr.append(None)
            v = b.get(s)
            arr[idx] = v["t"] if v else None
        b["hkey"] = key  # index.html이 히스토리 팝업에서 사용

    # 90일 넘으면 앞부분 자르기
    if len(hist["dates"]) > HISTORY_MAX_DAYS:
        cut = len(hist["dates"]) - HISTORY_MAX_DAYS
        hist["dates"] = hist["dates"][cut:]
        for bk in hist["books"].values():
            for cat_series in bk.get("series", {}).values():
                for k in list(cat_series.keys()):
                    cat_series[k] = cat_series[k][cut:]

    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


# ---------- 메인 ----------
def main():
    pid_ranks, prev_date = load_prev_pid_ranks()

    scraped = {}
    errors = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ko-KR")
        page.set_default_timeout(60000)
        for store in STORES:
            try:
                scraped[store] = SCRAPERS[store](page)
                print(f"[OK] {store}: {len(scraped[store])}건 수집")
            except Exception as e:
                scraped[store] = []
                errors[store] = str(e)
                print(f"[FAIL] {store}: {e}", file=sys.stderr)
        browser.close()

    books = build_books(scraped)
    apply_prev_ranks(books, pid_ranks)

    output = {
        "today": TODAY,
        "prev": prev_date,
        "surge_gap": 4,
        "categories": [{"id": "all", "label": "전체"}],
        "data": {"all": {"books": books}},
    }

    update_history(output)  # books에 hkey 채워짐 (output과 같은 객체 참조)

    with open(DATA_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료: 총 {len(books)}권, data.json / history.json 저장됨")
    if errors:
        print(f"일부 사이트 수집 실패: {list(errors.keys())} (다음 실행 때 재시도됩니다)")


if __name__ == "__main__":
    main()
