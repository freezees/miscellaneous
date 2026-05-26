"""
FCC OET Exhibits 自動下載器 - Playwright v12 (終極去重複 + 原始檔下載)
- 使用 context.request 直接抓取原始 PDF
- 導入 MD5 檔案內容雜湊比對，無視 FCC 的假 ID，100% 阻擋內容相同的重複報告
用法:
    python fcc_playwright.py --fcc-id UZ7KC50A15
"""

import argparse
import time
import re
import hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

DELAY        = 1.5
PAGE_TIMEOUT = 60000
SEARCH_URL   = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"


def sanitize(name: str) -> str:
    """清理檔名中的非法字元"""
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip() or "file"


def split_fcc_id(fcc_id: str):
    fcc_id = fcc_id.upper()
    if fcc_id[0].isalpha():
        return fcc_id[:3], fcc_id[3:]
    else:
        return fcc_id[:5], fcc_id[5:]


def download_pdf_direct(context, url: str, dest: Path, referer: str, global_seen_hashes: set) -> tuple[bool, bool]:
    """
    下載原始檔案並進行 MD5 比對
    回傳: (是否下載成功, 是否為重複內容)
    """
    try:
        response = context.request.get(
            url,
            headers={"Referer": referer},
            timeout=60000
        )
        
        if response.ok:
            body = response.body()
            
            # 使用 MD5 雜湊值來確認檔案內容是否與之前的完全一模一樣
            file_hash = hashlib.md5(body).hexdigest()
            
            if file_hash in global_seen_hashes:
                return True, True  # 成功抓取，但是是重複檔案
            
            # 這是新檔案，記錄它的 Hash 並寫入硬碟
            global_seen_hashes.add(file_hash)
            dest.write_bytes(body)
            return True, False
        else:
            print(f"         ⚠️  下載失敗，HTTP 狀態碼: {response.status}")
            return False, False

    except Exception as e:
        print(f"         ⚠️  下載異常：{e}")
        return False, False


def download_exhibits(context, exhibit_url, out_dir, record_label, global_seen_hashes):
    ok = fail = dup = 0

    tab = context.new_page()
    try:
        tab.goto(exhibit_url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
    except PWTimeout:
        print(f"    ❌ 清單頁載入逾時")
        tab.close()
        return 0, 1, 0

    anchors = tab.query_selector_all("a[href*='GetApplicationAttachment']")
    if not anchors:
        print(f"    ⚠️  無附件")
        tab.close()
        return 0, 0, 0

    files = []
    seen_hrefs = set()
    for a in anchors:
        href  = a.get_attribute("href") or ""
        label = (a.inner_text() or "file").strip()
        
        if href and href not in seen_hrefs:
            seen_hrefs.add(href)
            if not href.startswith("http"):
                href = "https://apps.fcc.gov" + href
            files.append((label, href))

    tab.close()
    
    if files:
        print(f"    📎 找到 {len(files)} 個附件，準備分析內容...")

    for label, url in files:
        filename = out_dir / f"{record_label}_{sanitize(label)}.pdf"
        
        # 為了避免舊檔案干擾，我們還是保留 exists 檢查
        if filename.exists():
            print(f"      ⏭️  本機已存在同名檔案，請確認是否為舊版截圖檔案！")
            continue

        print(f"      ⬇️  {label}")
        success, is_duplicate = download_pdf_direct(context, url, filename, exhibit_url, global_seen_hashes)

        if success:
            if is_duplicate:
                print(f"         ⏭️  跨項目重複 (內容 100% 相同)，拋棄不存檔！")
                dup += 1
            else:
                kb = filename.stat().st_size / 1024
                print(f"         ✅ {filename.name}  ({kb:.1f} KB)")
                ok += 1
        else:
            print(f"         ❌ 失敗：{label}")
            fail += 1

        time.sleep(DELAY)

    return ok, fail, dup


def run(fcc_id: str, out_dir: Path, headless: bool):
    out_dir.mkdir(parents=True, exist_ok=True)
    fcc_id = fcc_id.upper()
    grantee, product = split_fcc_id(fcc_id)

    print(f"📡 FCC ID: {fcc_id}  (grantee={grantee}, product={product})\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-http2"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page    = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

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

        print(f"   找到 {len(exhibit_links)} 筆大項目\n")

        if not exhibit_links:
            print("⚠️  找不到 Exhibit 連結")
            browser.close()
            return

        # ── 逐筆下載 ─────────────────────────────────────────────────
        total_ok = total_fail = total_dup = 0
        
        # 建立一個全域的 set，用來記錄所有「已經下載過的真實檔案內容 Hash 值」
        global_seen_hashes = set() 
        
        for idx, (label, url) in enumerate(exhibit_links, 1):
            record_label = f"R{idx:02d}"
            print(f"{'─'*50}")
            print(f"[{idx:02d}/{len(exhibit_links)}] {label or '(Exhibit)'}")
            
            ok, fail, dup = download_exhibits(context, url, out_dir, record_label, global_seen_hashes)
            
            total_ok   += ok
            total_fail += fail
            total_dup  += dup

        print(f"\n{'═'*55}")
        print(f"✅ 成功下載 {total_ok} 個新檔案")
        print(f"🗑️  成功阻擋 {total_dup} 個內容重複檔案")
        if total_fail > 0:
            print(f"❌ 失敗 {total_fail} 個")
        print(f"📁 儲存位置：{out_dir.resolve()}")
        browser.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="FCC Playwright 下載器 v12 (MD5 終極去重複)")
    p.add_argument("--fcc-id",   required=True,         help="FCC ID，例如 UZ7KC50A15")
    p.add_argument("--out",      default=None,            help="輸出資料夾")
    p.add_argument("--delay",    type=float, default=1.5, help="每檔間隔秒數")
    p.add_argument("--headless", action="store_true",     help="是否在背景執行瀏覽器")
    args = p.parse_args()

    DELAY   = args.delay
    out_dir = Path(args.out) if args.out else Path(f"fcc_{args.fcc_id.upper()}")
    
    run(args.fcc_id, out_dir, args.headless)