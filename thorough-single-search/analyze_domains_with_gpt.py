import csv
import json
import re
import sys
from pathlib import Path
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ------------------------------------------
# CONFIG
# ------------------------------------------

csv.field_size_limit(10485760)  # Allow up to 10MB fields

INPUT_TEXT_CSV = "domain_text.csv"
OUTPUT_ANALYSIS_CSV = "domain_analysis.csv"

MODEL_NAME = "gpt-5-nano"

MAX_INPUT_TEXT_CHARS = 9000        # Controls GPT cost
MAX_WORKERS = 5                    # How many GPT requests to run at once

client = OpenAI()

# ------------------------------------------
# CSV LOAD / SAVE HELPERS
# ------------------------------------------

def load_existing_text_csv(path: str):
    data, order = {}, []

    if not Path(path).is_file():
        return data, order

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row["domain"].lower()
            data[d] = {"url": row["url"], "text": row["text"]}
            order.append(d)
    return data, order


def load_existing_analysis_csv(path: str):
    if not Path(path).is_file():
        return {}
    analyzed = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            analyzed[row["domain"].lower()] = row
    return analyzed


def ensure_output_file(path: str):
    if not Path(path).is_file():
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "domain",
                "url",
                "for_sale",
                "buy_now_price",
                "all_prices_usd",
                "summary",
            ])

# ------------------------------------------
# GPT ANALYZER
# ------------------------------------------

def analyze_with_gpt(domain: str, text: str):
    """Runs the GPT analysis for a single domain."""

    text = text[:MAX_INPUT_TEXT_CHARS]

    prompt = f"""
You are analyzing the content of a website.

TASKS:
1. First, translate all content into English (for your understanding).
2. Detect if there is *any indication* that the domain/website is for sale.
3. Detect if there is a *buy-it-now price*.
4. Extract *all prices* mentioned and convert them into USD.
5. Provide a **single short sentence** describing what the website/domain is.

RESPONSE FORMAT (valid JSON only):
{{
  "for_sale": true/false,
  "buy_now_price": "number or NONE",
  "all_prices_usd": ["list of numbers or empty"],
  "summary": "short sentence"
}}

CONTENT:
{text}
"""

    try:
        resp = client.responses.create(
            model=MODEL_NAME,
            input=prompt,
        )
        raw = resp.output_text.strip()

    except Exception as e:
        print(f"[ERROR] GPT request failed for {domain}: {e}")
        return None

    # Try JSON decode
    try:
        return json.loads(raw)
    except Exception as e1:
        # Attempt to extract JSON manually
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception as e2:
                print(f"[WARN] Could not parse JSON for {domain}")
                print(f"  Raw response: {raw[:200]}")
                return None
        print(f"[WARN] Could not parse JSON for {domain}")
        print(f"  Raw response: {raw[:200]}")
        return None

# ------------------------------------------
# THREAD WORKER
# ------------------------------------------

def worker(domain: str, info: dict):
    """Thread-safe work function returning (domain, row) or None."""
    text = info["text"].strip()
    if not text:
        print(f"SKIP (no text): {domain}")
        return None

    print(f"→ Analyzing {domain} ...")
    result = analyze_with_gpt(domain, text)
    if not result:
        print(f"[WARN] Failed GPT analysis for {domain}")
        return None

    row = [
        domain,
        info["url"],
        str(result.get("for_sale", False)),
        result.get("buy_now_price", "NONE"),
        ", ".join(map(str, result.get("all_prices_usd", []))),
        result.get("summary", "")
    ]
    return domain, row

# ------------------------------------------
# MAIN
# ------------------------------------------

if __name__ == "__main__":
    text_data, order = load_existing_text_csv(INPUT_TEXT_CSV)
    analyzed = load_existing_analysis_csv(OUTPUT_ANALYSIS_CSV)

    print(f"Loaded {len(text_data)} domains.")
    print(f"Already analyzed: {len(analyzed)}")

    ensure_output_file(OUTPUT_ANALYSIS_CSV)

    # Filter domains that still need analysis
    to_process = [d for d in order if d not in analyzed]

    print(f"Need to analyze {len(to_process)} domains.\n")

    lock = Lock()  # protects file writes

    with open(OUTPUT_ANALYSIS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(worker, d, text_data[d]): d
                for d in to_process
            }

            for future in as_completed(futures):
                result = future.result()

                if not result:
                    continue

                domain, row = result

                with lock:
                    writer.writerow(row)
                    f.flush()

                print(f"✓ Saved {domain}")

    print("\n✓ All GPT analysis complete!")
