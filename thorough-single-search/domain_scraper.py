import csv
import itertools
import re
import string
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from bs4 import BeautifulSoup

# Increase CSV field size limit to handle large text content
csv.field_size_limit(10485760)  # 10MB limit

# ----------------- CONFIG -----------------

TLD_FILE = "data/two_letter_tlds.txt"
OUTPUT_TEXT_CSV = "domain_text.csv"

ignored_tlds = {".bl", ".bq", ".eh"}

MAX_WORKERS = 10   # increase threads safely

# ------------------------------------------


def load_tlds(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return [
            line.strip().lower()
            for line in f
            if line.strip() and line.strip().lower() not in ignored_tlds
        ]


def generate_domains(tlds, length: int = 1):
    base_chars = list(string.ascii_lowercase) + list(string.digits)
    labels = base_chars if length == 1 else [
        "".join(p) for p in itertools.product(base_chars, repeat=length)
    ]

    for label in labels:
        for tld in tlds:
            yield f"{label}{tld}"


def fetch_page(domain: str):
    headers = {"User-Agent": "DomainScraper/1.0 (+https://example.com/contact)"}
    urls = [f"https://{domain}", f"http://{domain}"]

    for url in urls:
        try:
            resp = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
            if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", "").lower():
                return url, resp.text
        except requests.RequestException:
            continue
    return None, None


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    return re.sub(r"\s+", " ", text).strip()


def load_existing_csv(path: str):
    if not Path(path).is_file():
        return {}

    existing = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            domain = row["domain"].lower()
            existing[domain] = {"url": row["url"], "text": row["text"]}
    return existing


def save_csv(path: str, data: dict):
    """Rewrite entire CSV safely (fast and keeps file consistent)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["domain", "url", "text"])
        for domain, info in data.items():
            writer.writerow([domain, info["url"], info["text"]])


def scrape_domain(domain: str):
    """This is the threaded worker function."""
    url, html = fetch_page(domain)
    if not url or not html:
        return domain, "", ""
    text = extract_visible_text(html)
    return domain, url, text


# ------------- MAIN (MULTITHREADED) -------------


if __name__ == "__main__":
    tlds = load_tlds(TLD_FILE)

    existing = load_existing_csv(OUTPUT_TEXT_CSV)
    print(f"Loaded {len(existing)} existing entries.")

    lock = Lock()  # protects in-memory dict + CSV writes

    all_domains = list(generate_domains(tlds, length=1))

    # Only send domains that need scraping
    domains_to_scrape = [
        d for d in all_domains
        if d not in existing or not existing[d]["text"]
    ]

    print(f"Need to scrape: {len(domains_to_scrape)} domains")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scrape_domain, dom): dom
            for dom in domains_to_scrape
        }

        for future in as_completed(futures):
            domain, url, text = future.result()

            with lock:
                existing[domain] = {"url": url, "text": text}
                save_csv(OUTPUT_TEXT_CSV, existing)

            print(f"Updated {domain} (text_length={len(text)})")
