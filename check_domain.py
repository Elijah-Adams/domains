#!/usr/bin/env python3
"""
check_domain.py — generic domain availability check via RDAP with WHOIS fallback.

Usage:
  python check_domain.py example.com
  python check_domain.py example.com example.net café.fr

Exit codes: 0 = all queries completed (see per-domain results)
"""

import sys, os, json, time, socket, ssl, urllib.request, urllib.error
from urllib.parse import urljoin

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache")
CACHE_PATH = os.path.join(CACHE_DIR, "rdap_bootstrap_dns.json")
CACHE_TTL_SECONDS = 7 * 24 * 3600  # refresh weekly

TIMEOUT = 12  # seconds for HTTP/WHOIS
USER_AGENT = "domain-check/1.0 (+https://iana.org; generic RDAP/WHOIS client)"

def to_ascii_domain(name: str) -> str:
    # Handle IDNs (e.g., café.fr) using stdlib idna codec
    return name.encode("idna").decode("ascii")

def load_bootstrap():
    # cache the RDAP bootstrap to avoid hitting IANA every run
    if os.path.exists(CACHE_PATH):
        try:
            st = os.stat(CACHE_PATH)
            if (time.time() - st.st_mtime) < CACHE_TTL_SECONDS:
                with open(CACHE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass

    req = urllib.request.Request(BOOTSTRAP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data

def rdap_base_for_tld(bootstrap: dict, tld: str) -> str | None:
    # Bootstrap format: "services": [ [list-of-tlds], [list-of-base-urls] ]
    tld = tld.lower().lstrip(".")
    for svc in bootstrap.get("services", []):
        tlds, bases = svc
        if tld in (x.lower() for x in tlds):
            # prefer https
            for b in bases:
                if b.lower().startswith("https://"):
                    return b.rstrip("/")
            return bases[0].rstrip("/") if bases else None
    return None

def http_get(url: str) -> tuple[int, bytes, dict] | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return (r.getcode(), r.read(), dict(r.headers))
    except urllib.error.HTTPError as e:
        return (e.code, e.read(), dict(e.headers))
    except Exception:
        return None

def rdap_check(domain: str, rdap_base: str) -> tuple[str, str]:
    """
    Returns (status, detail)
    status ∈ {"registered","not_registered","unknown"}
    """
    # Some servers expect /domain/{name}
    url = urljoin(rdap_base + "/", "domain/" + domain)
    res = http_get(url)
    if res is None:
        return ("unknown", f"RDAP request failed")
    code, body, headers = res
    if code == 404:
        return ("not_registered", "RDAP 404 Not Found")
    if code == 200:
        # If it’s a valid RDAP object, we assume registered
        try:
            obj = json.loads(body.decode("utf-8", errors="ignore"))
            if isinstance(obj, dict) and obj.get("objectClassName") in ("domain", "domainSearchResults"):
                return ("registered", "RDAP 200 domain object returned")
            # Some servers still return useful JSON—treat as registered if ambiguous
            return ("registered", "RDAP 200 (assumed domain object)")
        except Exception:
            return ("registered", "RDAP 200 (non-JSON?)")
    if code in (400, 401, 403, 405, 429, 500, 503):
        return ("unknown", f"RDAP HTTP {code}")
    return ("unknown", f"RDAP unexpected HTTP {code}")

# --- WHOIS helpers ---

def whois_query(server: str, query: str, port: int = 43) -> str | None:
    try:
        with socket.create_connection((server, port), timeout=TIMEOUT) as sock:
            sock.sendall((query + "\r\n").encode("utf-8", errors="ignore"))
            chunks = []
            sock.settimeout(TIMEOUT)
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return None

def whois_server_for_tld(tld: str) -> str | None:
    # Ask whois.iana.org for the TLD and parse the "whois:" line
    resp = whois_query("whois.iana.org", tld)
    if not resp:
        return None
    for line in resp.splitlines():
        if line.lower().startswith("whois:"):
            srv = line.split(":", 1)[1].strip()
            # some lines include URL; expect hostname only
            return srv.replace("http://", "").replace("https://", "").strip().strip("/")
    return None

def whois_check(domain: str) -> tuple[str, str]:
    """
    Returns (status, detail) via WHOIS heuristic.
    """
    tld = domain.rsplit(".", 1)[-1].lower()
    server = whois_server_for_tld(tld)
    if not server:
        return ("unknown", "No WHOIS server for TLD via whois.iana.org")

    resp = whois_query(server, domain)
    if not resp:
        return ("unknown", f"WHOIS query to {server} failed")

    # Very common "not found" patterns across registries
    not_found_markers = [
        "no match for", "not found", "no entries found", "status: free",
        "available", "is not registered", "no data found", "domain you requested is not known"
    ]
    lower = resp.lower()
    if any(marker in lower for marker in not_found_markers):
        return ("not_registered", f"WHOIS: indicates not found ({server})")
    # Otherwise assume registered if contact/status fields show up
    if any(k in lower for k in ["registrar", "creation date", "updated date", "name server", "status:"]):
        return ("registered", f"WHOIS: records present ({server})")
    return ("unknown", f"WHOIS: inconclusive ({server})")

def check_one(name: str, bootstrap: dict) -> dict:
    original = name.strip()
    if not original or "." not in original:
        return {"domain": original, "status": "error", "detail": "Invalid domain syntax"}
    ascii_domain = to_ascii_domain(original)
    tld = ascii_domain.rsplit(".", 1)[-1].lower()

    rdap_base = rdap_base_for_tld(bootstrap, tld)
    if rdap_base:
        status, detail = rdap_check(ascii_domain, rdap_base)
        if status in ("registered", "not_registered"):
            return {"domain": original, "status": status, "source": "RDAP", "detail": detail}
        # fall through on unknown
    else:
        detail = "No RDAP base for TLD in IANA bootstrap"

    # WHOIS fallback
    w_status, w_detail = whois_check(ascii_domain)
    return {"domain": original, "status": w_status, "source": "WHOIS", "detail": w_detail}

def main(argv):
    if len(argv) < 2:
        print("Usage: python check_domain.py <domain> [<domain> ...]")
        return 2

    try:
        bootstrap = load_bootstrap()
    except Exception as e:
        print(f"Warning: failed to load RDAP bootstrap ({e}). WHOIS-only fallback will be used.")
        bootstrap = {"services": []}

    any_fail = False
    for d in argv[1:]:
        result = check_one(d, bootstrap)
        status = result.get("status")
        source = result.get("source", "")
        detail = result.get("detail", "")
        print(f"{result['domain']}: {status.upper()}  [{source}]  {detail}")
        if status in ("unknown", "error"):
            any_fail = True

    return 1 if any_fail else 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
