"""
FCC OET Exhibits 自動下載器 - Playwright v10
- 用 page.pdf() 把每個附件頁面直接列印成 PDF
- 完全不需要下載按鈕，不會被擋
用法:
    python fcc_playwright.py --fcc-id UZ7KC50A15
"""

import argparse
import time
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DELAY        = 1.5
PAGE_TIMEOUT = 60000
SEARCH_URL   = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"


def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "file"


def split_fcc_id(fcc_id: str):
    fcc_id = fcc_id.upper()
    if fcc_id[0].isalpha():
        return fcc_id[:3], fcc_id[3:]
    else:
        return fcc_id[:5], fcc_id[5:]


def print_page_as_pdf(context, url: str, dest: Path, referer: str) -> bool:
    """開新分頁前往 URL，等載入完成後用 page.pdf() 列印成 PDF"""
    tab = context.new_page()
    try:
        tab.set_extra_http_headers({"Referer": referer})
        tab.goto(url, wait_until="networkidle", timeout=60000)

        # 等 PDF viewer 或頁面內容完全載入
        time.sleep(2)

        # 如果是 PDF viewer（瀏覽器內嵌），先嘗試取得真實 PDF URL
        # 有些 FCC 頁面會 redirect 到真實 PDF
        final_url = tab.url

        # 直接列印當前頁面為 PDF
        pdf_bytes = tab.pdf(
            format          = "A4",
            print_background = True,
            margin          = {"top": "10mm", "bottom": "10mm",
                               "left": "10mm", "right": "10mm"},
        )
        dest.write_bytes(pdf_bytes)
        return True

    except PWTimeout:
        print(f"         ⚠️  頁面載入逾時")
        return False
    except Exception as e:
        print(f"         ⚠️  列印失敗：{e}")
        return False
    finally:
        tab.close()


def download_exhibits(context, exhibit_url, out_dir, record_label):
    ok = fail = 0

    # 用主頁面開清單
    tab = context.new_page()
    try:
        tab.goto(exhibit_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    except PWTimeout:
        print(f"    ❌ 清單頁載入逾時")
        tab.close()
        return 0, 1

    anchors = tab.query_selector_all("a[href*='GetApplicationAttachment']")
    if not anchors:
        print(f"    ⚠️  無附件")
        tab.close()
        return 0, 0

    files = []
    seen  = set()
    for a in anchors:
        href  = a.get_attribute("href") or ""
        label = (a.inner_text() or "file").strip()
        if href and href not in seen:
            seen.add(href)
            if not href.startswith("http"):
                href = "https://apps.fcc.gov" + href
            files.append((label, href))

    tab.close()
    print(f"    📎 {len(files)} 個附件")

    for label, url in files:
        filename = out_dir / f"{record_label}_{sanitize(label)}.pdf"
        if filename.exists():
            print(f"      ⏭️  已存在：{filename.name}")
            ok += 1
            continue

        print(f"      ⬇️  {label}")
        success = print_page_as_pdf(context, url, filename, exhibit_url)

        if success:
            kb = filename.stat().st_size / 1024
            print(f"         ✅ {filename.name}  ({kb:.1f} KB)")
            ok += 1
        else:
            print(f"         ❌ 失敗：{label}")
            fail += 1

        time.sleep(DELAY)

    return ok, fail


def run(fcc_id: str, out_dir: Path, headless: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    fcc_id = fcc_id.upper()
    grantee, product = split_fcc_id(fcc_id)

    print(f"📡 FCC ID: {fcc_id}  (grantee={grantee}, product={product})\n")

    with sync_playwright() as p:
        # page.pdf() 只能在 headless 模式下使用
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-http2"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page    = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        # ── 搜尋 ─────────────────────────────────────────────────────
        print("🔍 步驟1：搜尋 FCC ID...")
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_load_state("networkidle", timeout=20000)
        page.fill("input[name='grantee_code']", grantee)
        page.fill("input[name='product_code']",  product)
        try:
            page.fill("input[name='show_records']", "100")
        except Exception:
            pass
        page.click("input[value='Start Search']")
        page.wait_for_load_state("networkidle", timeout=30000)
        print("   ✅ 搜尋完成\n")

        # ── 收集 Exhibit 連結 ─────────────────────────────────────────
        print("📋 步驟2：收集 Exhibit 清單連結...")
        exhibit_links = []
        seen_urls     = set()
        for a in page.query_selector_all("a[href*='ViewExhibitReport']"):
            href  = a.get_attribute("href") or ""
            label = a.inner_text().strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = "https://apps.fcc.gov" + href
            if href not in seen_urls:
                seen_urls.add(href)
                exhibit_links.append((label, href))

        print(f"   找到 {len(exhibit_links)} 筆\n")

        if not exhibit_links:
            print("⚠️  找不到 Exhibit 連結")
            browser.close()
            return

        # ── 逐筆下載 ─────────────────────────────────────────────────
        total_ok = total_fail = 0
        for idx, (label, url) in enumerate(exhibit_links, 1):
            record_label = f"R{idx:02d}"
            print(f"{'─'*50}")
            print(f"[{idx:02d}/{len(exhibit_links)}] {label or '(Exhibit)'}")
            ok, fail = download_exhibits(context, url, out_dir, record_label)
            total_ok   += ok
            total_fail += fail

        print(f"\n{'═'*55}")
        print(f"✅ 成功 {total_ok} 個　❌ 失敗 {total_fail} 個")
        print(f"📁 儲存位置：{out_dir.resolve()}")
        browser.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="FCC Playwright 下載器 v10")
    p.add_argument("--fcc-id",   required=True,          help="FCC ID，例如 UZ7KC50A15")
    p.add_argument("--out",      default=None,            help="輸出資料夾")
    p.add_argument("--delay",    type=float, default=1.5, help="每檔間隔秒數")
    args = p.parse_args()

    DELAY   = args.delay
    out_dir = Path(args.out) if args.out else Path(f"fcc_{args.fcc_id.upper()}")
    run(args.fcc_id, out_dir, False)
