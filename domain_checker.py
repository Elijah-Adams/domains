import os
import csv
import time
import re
import string
import itertools
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from openai import OpenAI

# ----------------- CONFIG -----------------

TLD_FILE = "data/two_letter_tlds.txt"
OUTPUT_CSV = "domain_prices.csv"

ignored_tlds = {".bl", ".bq", ".eh"}

# Delay between OpenAI calls (seconds) to be nice to rate limits
OPENAI_SLEEP_SECONDS = 0.2

# Max characters of page text to send to GPT (controls cost)
MAX_TEXT_CHARS = 8000

# Number of concurrent workers (tune this!)
MAX_WORKERS = 5

# ------------------------------------------

client = OpenAI()


def load_tlds(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return [
            line.strip().lower()
            for line in f
            if line.strip() and line.strip().lower() not in ignored_tlds
        ]


def generate_domains(tlds, length=1):
    base_chars = list(string.ascii_lowercase) + list(string.digits)

    if length == 1:
        labels = base_chars
    else:
        labels = ["".join(p) for p in itertools.product(base_chars, repeat=length)]

    for label in labels:
        for tld in tlds:
            yield f"{label}{tld}"


def fetch_page(domain: str):
    """
    Try HTTPS first, then HTTP. Returns (url, html_text) or (None, None).
    """
    headers = {
        "User-Agent": "DomainPriceChecker/1.0 (+https://example.com/your-contact)"
    }
    urls = [f"https://{domain}", f"http://{domain}"]

    for url in urls:
        try:
            resp = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
            if resp.status_code == 200 and "text/html" in resp.headers.get(
                "Content-Type", ""
            ):
                return url, resp.text
        except requests.RequestException:
            continue

    return None, None


def extract_visible_text(html: str) -> str:
    """
    Strip scripts/styles and return a cleaned text string.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    text = re.sub(r"\s+", " ", text)  # collapse whitespace
    return text.strip()


def ensure_output_file(path: str):
    """
    Ensure CSV exists and has a header row.
    """
    file_exists = Path(path).is_file()
    if not file_exists:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["domain", "url", "price"])  # price can be NONE / NO_TEXT / NO_SITE


def ask_gpt_for_price(page_text: str) -> str:
    # Truncate to control cost
    page_text = page_text[:MAX_TEXT_CHARS]

    prompt = (
        "Tell me the price if there is one in this. "
        "Do not include commas, currency symbols, or words—"
        "only return a number. "
        "If there is no price return NONE. "
        f"{page_text}"
    )

    response = client.responses.create(
        model="gpt-4.1-nano",   # or gpt-5-nano if you prefer
        input=prompt,
        max_output_tokens=32,
    )

    # Print raw model output for debugging
    print("RAW GPT RESPONSE:", repr(response.output_text))

    raw = response.output_text
    if not raw:
        return "NONE"

    cleaned = (
        raw.replace("$", "")
           .replace(",", "")
           .replace("€", "")
           .replace("£", "")
           .strip()
    )

    if cleaned.upper() == "NONE":
        return "NONE"

    if any(ch.isdigit() for ch in cleaned):
        return cleaned

    return "NONE"


# ------------- NEW: per-domain worker -------------

def process_domain(domain: str):
    """
    Work function that can run in a thread.
    Returns (domain, url, price).
    """
    print(f"Thread: checking {domain} ...")

    url, html = fetch_page(domain)
    if not url or not html:
        # No site at all
        return domain, "", "NO_SITE"

    text = extract_visible_text(html)
    if not text:
        # Site but no useful text
        return domain, url, "NO_TEXT"

    price = ask_gpt_for_price(text)

    # Gentle rate limiting per thread
    time.sleep(OPENAI_SLEEP_SECONDS)

    print(f"Thread: {domain} -> {price}")
    return domain, url, price


# ------------- MAIN -------------

if __name__ == "__main__":
    tlds = load_tlds(TLD_FILE)
    ensure_output_file(OUTPUT_CSV)

    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # ThreadPoolExecutor handles parallelism
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # executor.map lazily feeds domains, so you don't create all futures at once
            for idx, (domain, url, price) in enumerate(
                executor.map(process_domain, generate_domains(tlds)),
                start=1,
            ):
                print(f"[{idx}] Writing result for {domain}: {price}")
                writer.writerow([domain, url, price])
                f.flush()
