import csv
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Increase CSV field size limit to handle large text content
csv.field_size_limit(10485760)  # 10MB limit

# ----------------- CONFIG -----------------

INPUT_OUTPUT_CSV = "domain_text_copy.csv"
PAGE_LOAD_TIMEOUT_MS = 15000
WAIT_AFTER_LOAD_MS = 1000
MIN_TEXT_LENGTH = 1
MAX_WORKERS = 16   # number of concurrent browser contexts

# ------------------------------------------


def load_existing_csv(path: str):
    existing = {}
    order = []

    if not Path(path).is_file():
        return existing, order

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = (row.get("domain") or "").lower()
            if not domain:
                continue
            existing[domain] = {
                "url": row.get("url") or "",
                "text": row.get("text") or "",
            }
            order.append(domain)

    return existing, order


def save_csv(path: str, data: dict, order: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["domain", "url", "text"])
        for domain in order:
            info = data.get(domain, {"url": "", "text": ""})
            wr.writerow([domain, info["url"], info["text"]])


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return re.sub(r"\s+", " ", text).strip()


def headless_task(domain: str, existing_url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        browser_context = browser.new_context()
        page = browser_context.new_page()

        candidate_urls = []
        if existing_url:
            candidate_urls.append(existing_url)
        candidate_urls.extend([
            f"https://{domain}",
            f"http://{domain}",
        ])

        for url in candidate_urls:
            try:
                resp = page.goto(url, wait_until="networkidle",
                                 timeout=PAGE_LOAD_TIMEOUT_MS)
                page.wait_for_timeout(WAIT_AFTER_LOAD_MS)

                # Ensure HTML
                if resp is not None:
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "text/html" not in ctype:
                        continue

                html = page.content()
                text = extract_visible_text(html)
                final_url = page.url or url

                if len(text) > 0:
                    browser_context.close()
                    browser.close()
                    return domain, final_url, text

            except Exception:
                continue

        browser_context.close()
        browser.close()
        return domain, existing_url, ""


if __name__ == "__main__":
    existing, order = load_existing_csv(INPUT_OUTPUT_CSV)
    print(f"Loaded {len(existing)} entries.")

    # Determine which domain needs rerendering
    domains_to_process = [
        d for d in order
        if len(existing[d]["text"]) < MIN_TEXT_LENGTH
    ]
    print(f"Found {len(domains_to_process)} domains with missing/short text (< {MIN_TEXT_LENGTH}).")

    if not domains_to_process:
        print("Nothing to do.")
        exit()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(headless_task, domain, existing[domain]["url"]): domain
            for domain in domains_to_process
        }

        for future in as_completed(futures):
            domain, url, text = future.result()

            existing[domain]["url"] = url
            existing[domain]["text"] = text

            save_csv(INPUT_OUTPUT_CSV, existing, order)
            print(f"✓ Updated {domain} (len={len(text)})")

    print("Done.")
