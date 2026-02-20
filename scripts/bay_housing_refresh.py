#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from typing import Dict, List

NOTION_API_VERSION = "2022-06-28"
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bay_housing_latest.json")

MOVE_WINDOW = "Aug-Sep 2026"
MAX_RENT = 8000
STRETCH_MAX = 9000
MIN_BEDS = 2
MIN_BATHS = 2
ALLOWED_PROPERTY_TYPES = {"house", "townhouse"}

AREAS = ["Woodside", "Portola Valley", "Los Altos Hills", "Saratoga", "Los Gatos", "Cupertino"]

SEARCH_URLS = {
    # Zillow paused by request for now; revisit later once DB product is stable.
    "redfin": [
        "https://www.redfin.com/city/14972/CA/Los-Gatos/apartments-for-rent",
        "https://www.redfin.com/city/17420/CA/Saratoga/apartments-for-rent",
    ],
    "realtor": [
        "https://www.realtor.com/apartments/Los-Gatos_CA",
        "https://www.realtor.com/apartments/Saratoga_CA",
    ],
}


def fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def norm_price(s: str) -> float | None:
    m = re.search(r"\$\s*([0-9][0-9,]*)", s or "")
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def parse_beds_baths(text: str) -> tuple[float | None, float | None]:
    t = (text or "").lower()
    b = re.search(r"(\d+(?:\.\d+)?)\s*(?:bd|beds?)", t)
    ba = re.search(r"(\d+(?:\.\d+)?)\s*(?:ba|baths?)", t)
    return (float(b.group(1)) if b else None, float(ba.group(1)) if ba else None)


def infer_property_type(raw: str, url: str, name: str = "") -> str:
    t = f"{raw} {url} {name}".lower()
    if "townhouse" in t or "townhome" in t:
        return "townhouse"
    if "single family" in t or "house for rent" in t or "/homedetails/" in t:
        return "house"
    if "apartment" in t:
        return "apartment"
    if "condo" in t:
        return "condo"
    return "unknown"


def normalize_text(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def likely_street_address(s: str) -> bool:
    s = (s or "")
    return bool(re.search(r"\b\d{1,6}\s+[a-z0-9].+", s, flags=re.I))


def address_from_url(url: str) -> str:
    # Zillow home details: /homedetails/28-Meadow-Rd-Woodside-CA-94062/...
    m = re.search(r"/homedetails/([^/]+)/", url)
    if m:
        slug = m.group(1)
        s = slug.replace("-", " ")
        s = re.sub(r"\bCA\b", "CA", s)
        return s

    # Realtor details path often contains address-like slug
    m = re.search(r"/details/([^/?]+)", url)
    if m:
        return m.group(1).replace("_", " ").replace("-", " ")

    return ""


def infer_city(text: str) -> str:
    t = (text or "").lower()
    for area in AREAS:
        if area.lower() in t:
            return area
    if "redwood city" in t:
        return "Redwood City"
    if "san jose" in t:
        return "San Jose"
    if "mountain view" in t:
        return "Mountain View"
    if "sunnyvale" in t:
        return "Sunnyvale"
    return ""


def nature_score(area: str, raw: str) -> float:
    score = 0.0
    text = f"{area} {raw}".lower()
    if any(k in text for k in ["foothill", "hills", "ridge", "trail", "open space", "park", "mountain"]):
        score += 3
    if any(k in text for k in ["woodside", "portola valley", "los altos hills", "saratoga", "los gatos"]):
        score += 2
    return score


def commute_placeholder_score(area: str) -> float:
    a = (area or "").lower()
    if any(k in a for k in ["los altos hills", "cupertino", "saratoga"]):
        return 4
    if any(k in a for k in ["los gatos", "woodside", "portola valley"]):
        return 3
    if any(k in a for k in ["mountain view", "sunnyvale"]):
        return 5
    return 2


def canonical_key(item: Dict) -> str:
    addr = normalize_text(item.get("address", ""))
    if likely_street_address(addr):
        return f"addr:{addr}"

    name = normalize_text(item.get("property_name", ""))
    city = normalize_text(item.get("city", ""))
    if name and city:
        return f"namecity:{name}|{city}"

    # fallback
    url_key = normalize_text(re.sub(r"https?://", "", item.get("url", "")))
    return f"url:{url_key[:140]}"


def score(item: Dict) -> float:
    s = 0.0
    price = item.get("price")
    beds = item.get("beds")
    baths = item.get("baths")

    if price is not None:
        if price <= MAX_RENT:
            s += 4
        elif price <= STRETCH_MAX:
            s += 2
    if beds is not None:
        s += 2 if beds >= 3 else 1 if beds >= 2 else 0
    if baths is not None and baths >= 2:
        s += 1

    if item.get("dog_friendly") == "yes":
        s += 2
    if item.get("parking") == "yes":
        s += 2

    s += item.get("nature_score", 0)
    s += item.get("commute_score", 0)
    return round(s, 2)


def passes_hard_filters(item: Dict) -> bool:
    price, beds, baths = item.get("price"), item.get("beds"), item.get("baths")
    ptype = (item.get("property_type") or "unknown").lower()
    if ptype not in ALLOWED_PROPERTY_TYPES:
        return False
    if price is not None and price > STRETCH_MAX:
        return False
    if beds is not None and beds < MIN_BEDS:
        return False
    if baths is not None and baths < MIN_BATHS:
        return False
    return True


def parse_candidates(source: str, html: str, base_url: str) -> List[Dict]:
    patterns = {
        "zillow": r'https://www\\.zillow\\.com/(?:homedetails|apartments)/[^"\\s<>]+',
        "redfin": r'https://www\\.redfin\\.com/CA/[^"\\s<>]+',
        "realtor": r'/rentals/details/[^"\\s<>?]+'
    }

    p = re.compile(patterns[source])
    urls = list(dict.fromkeys(p.findall(html)))[:100]

    rows: List[Dict] = []
    for u in urls:
        if source == "realtor" and u.startswith("/"):
            u = "https://www.realtor.com" + u

        # sample surrounding text for lightweight parsing
        m = re.search(re.escape(u) + r".{0,550}", html, flags=re.DOTALL)
        raw = (m.group(0) if m else "")[:900]

        price = norm_price(raw)
        beds, baths = parse_beds_baths(raw)
        address = address_from_url(u)
        city = infer_city(f"{address} {raw} {base_url}")

        # property name heuristic from URL slug
        prop_name = ""
        mname = re.search(r"/(apartments|details)/([^/?]+)", u)
        if mname:
            prop_name = mname.group(2).replace("-", " ").replace("_", " ")

        ptype = infer_property_type(raw, u, prop_name)

        rows.append(
            {
                "listing_id": urllib.parse.quote(u, safe=""),
                "source": source,
                "url": u,
                "property_name": prop_name[:220],
                "address": address[:220],
                "city": city,
                "property_type": ptype,
                "price": price,
                "beds": beds,
                "baths": baths,
                "dog_friendly": "yes" if re.search(r"pets?ok|pet friendly|dogs?", raw, flags=re.I) else "maybe",
                "parking": "yes" if re.search(r"parking|garage", raw, flags=re.I) else "maybe",
                "nature_score": nature_score(city, raw),
                "commute_score": commute_placeholder_score(city),
                "raw": raw,
            }
        )

    return rows


def quality_assessment(item: Dict) -> tuple[str, str]:
    reasons = []
    if not item.get("canonical_key"):
        reasons.append("missing canonical key")
    if not (item.get("address") or item.get("property_name")):
        reasons.append("missing address/name")
    # single URL model: rely on Primary URL only
    if not item.get("sources"):
        reasons.append("missing sources")
    if item.get("price") is None:
        reasons.append("missing price")
    if item.get("beds") is None:
        reasons.append("missing beds")
    if item.get("baths") is None:
        reasons.append("missing baths")
    ptype = (item.get("property_type") or "unknown").lower()
    if ptype not in ALLOWED_PROPERTY_TYPES:
        reasons.append(f"property type not allowed: {ptype}")

    if reasons:
        return "fail", "; ".join(reasons)
    return "pass", "ready"


def merge_duplicates(items: List[Dict]) -> List[Dict]:
    by_key: Dict[str, Dict] = {}
    for item in items:
        key = canonical_key(item)
        item["canonical_key"] = key
        item["match_score"] = score(item)

        if key not in by_key:
            merged = dict(item)
            merged["sources"] = sorted(list({item["source"]}))
            merged["source_urls"] = [item["url"]]
            merged["listing_ids"] = [item["listing_id"]]
            by_key[key] = merged
            continue

        cur = by_key[key]
        # preserve best-known numeric fields
        for fld in ["price", "beds", "baths"]:
            if cur.get(fld) is None and item.get(fld) is not None:
                cur[fld] = item[fld]

        # keep an address if missing
        if not cur.get("address") and item.get("address"):
            cur["address"] = item["address"]

        # merge provenance
        cur["sources"] = sorted(list(set(cur.get("sources", [])) | {item["source"]}))
        cur["source_urls"] = sorted(list(set(cur.get("source_urls", [])) | {item["url"]}))
        cur["listing_ids"] = sorted(list(set(cur.get("listing_ids", [])) | {item["listing_id"]}))

        # choose higher score representative
        if item["match_score"] > cur.get("match_score", 0):
            for fld in ["property_name", "city", "property_type", "dog_friendly", "parking", "nature_score", "commute_score", "match_score"]:
                cur[fld] = item.get(fld)

    merged = list(by_key.values())
    merged = [m for m in merged if passes_hard_filters(m)]
    for m in merged:
        q, notes = quality_assessment(m)
        m["data_quality"] = q
        m["quality_notes"] = notes
    merged.sort(key=lambda x: x.get("match_score", 0), reverse=True)
    return merged


def notion_request(method: str, path: str, token: str, payload: dict | None = None):
    req = urllib.request.Request(
        f"https://api.notion.com{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        },
        data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body[:500]}")


def notion_ensure_schema(token: str, db_id: str) -> str:
    db = notion_request("GET", f"/v1/databases/{db_id}", token)
    props = db.get("properties", {})
    title_prop_name = next((k for k, v in props.items() if v.get("type") == "title"), "Name")

    wanted = {
        "Canonical Key": {"rich_text": {}},
        "Sources": {"multi_select": {"options": [{"name": "zillow"}, {"name": "redfin"}, {"name": "realtor"}]}} ,
        "Primary URL": {"url": {}},
        "Address": {"rich_text": {}},
        "City": {"rich_text": {}},
        "Property Type": {"select": {"options": [{"name": "house"}, {"name": "townhouse"}, {"name": "apartment"}, {"name": "condo"}, {"name": "unknown"}]}} ,
        "Home/Townhome Preferred": {"select": {"options": [{"name": "yes"}, {"name": "no"}]}} ,
        "Price": {"number": {"format": "dollar"}},
        "Beds": {"number": {"format": "number"}},
        "Baths": {"number": {"format": "number"}},
        "Dog Friendly": {"select": {"options": [{"name": "yes"}, {"name": "maybe"}, {"name": "no"}]}} ,
        "Parking": {"select": {"options": [{"name": "yes"}, {"name": "maybe"}, {"name": "no"}]}} ,
        "Move Window": {"rich_text": {}},
        "Stretch": {"select": {"options": [{"name": "no"}, {"name": "yes"}]}} ,
        "Commute Score": {"number": {"format": "number"}},
        "Nature Score": {"number": {"format": "number"}},
        "Match Score": {"number": {"format": "number"}},
        "Data Quality": {"select": {"options": [{"name": "pass"}, {"name": "fail"}]}} ,
        "Quality Notes": {"rich_text": {}},
        "Last Seen": {"date": {}},
    }

    missing = {k: v for k, v in wanted.items() if k not in props}
    if missing:
        notion_request("PATCH", f"/v1/databases/{db_id}", token, {"properties": missing})

    return title_prop_name


def notion_find_by_canonical_key(token: str, db_id: str, key: str):
    payload = {"filter": {"property": "Canonical Key", "rich_text": {"equals": key}}, "page_size": 1}
    res = notion_request("POST", f"/v1/databases/{db_id}/query", token, payload)
    rows = res.get("results", [])
    return rows[0]["id"] if rows else None


def notion_props(item: Dict, title_prop_name: str, seen_date: str) -> Dict:
    title = item.get("address") or item.get("property_name") or "Bay Area rental"
    stretch = "yes" if (item.get("price") and item["price"] > MAX_RENT) else "no"

    rt = lambda s: [{"text": {"content": (s or "")[:1800]}}]

    ptype = (item.get("property_type") or "unknown").lower()
    preferred = "yes" if ptype in ALLOWED_PROPERTY_TYPES else "no"

    return {
        title_prop_name: {"title": [{"text": {"content": title[:1800]}}]},
        "Canonical Key": {"rich_text": rt(item.get("canonical_key", ""))},
        "Sources": {"multi_select": [{"name": s} for s in item.get("sources", [])]},
        "Primary URL": {"url": item.get("url", "")},
        "Address": {"rich_text": rt(item.get("address", ""))},
        "City": {"rich_text": rt(item.get("city", ""))},
        "Property Type": {"select": {"name": ptype}},
        "Home/Townhome Preferred": {"select": {"name": preferred}},
        "Price": {"number": item.get("price")},
        "Beds": {"number": item.get("beds")},
        "Baths": {"number": item.get("baths")},
        "Dog Friendly": {"select": {"name": item.get("dog_friendly", "maybe")}},
        "Parking": {"select": {"name": item.get("parking", "maybe")}},
        "Move Window": {"rich_text": rt(MOVE_WINDOW)},
        "Stretch": {"select": {"name": stretch}},
        "Commute Score": {"number": item.get("commute_score", 0)},
        "Nature Score": {"number": item.get("nature_score", 0)},
        "Match Score": {"number": item.get("match_score", 0)},
        "Data Quality": {"select": {"name": item.get("data_quality", "fail")}},
        "Quality Notes": {"rich_text": rt(item.get("quality_notes", ""))},
        "Last Seen": {"date": {"start": seen_date}},
    }


def sync_to_notion(items: List[Dict], db_id: str) -> str:
    token = os.getenv("NOTION_API_TOKEN", "").strip()
    if not token:
        return "Notion sync skipped: missing NOTION_API_TOKEN"

    title_prop_name = notion_ensure_schema(token, db_id)
    seen_date = dt.datetime.now(dt.timezone.utc).date().isoformat()

    upserts = 0
    skipped_quality = 0
    for item in items:
        if item.get("data_quality") != "pass":
            skipped_quality += 1
            continue

        page_id = notion_find_by_canonical_key(token, db_id, item["canonical_key"])
        props = notion_props(item, title_prop_name, seen_date)
        if page_id:
            notion_request("PATCH", f"/v1/pages/{page_id}", token, {"properties": props})
        else:
            notion_request("POST", "/v1/pages", token, {"parent": {"database_id": db_id}, "properties": props})
        upserts += 1

    return f"Notion upserts (deduped): {upserts}; skipped quality gate: {skipped_quality}"


def run(max_items: int, no_notion: bool, db_id: str | None):
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    raw_items: List[Dict] = []
    diagnostics = []

    for source, urls in SEARCH_URLS.items():
        for url in urls:
            try:
                html = fetch_html(url)
                items = parse_candidates(source, html, url)
                diagnostics.append({"source": source, "url": url, "found": len(items)})
                raw_items.extend(items)
            except Exception as e:
                diagnostics.append({"source": source, "url": url, "error": str(e)[:180]})

    merged = merge_duplicates(raw_items)
    if max_items > 0:
        merged = merged[:max_items]

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at_utc": fetched_at,
                "requirements": {
                    "move_window": MOVE_WINDOW,
                    "max_rent": MAX_RENT,
                    "stretch_max": STRETCH_MAX,
                    "min_beds": MIN_BEDS,
                    "min_baths": MIN_BATHS,
                    "property_type": "house or townhouse only",
                    "dog_friendly": "required",
                    "parking": "required",
                    "commute": "placeholder score for <=30 min to Mountain View and Sunnyvale",
                },
                "diagnostics": diagnostics,
                "count_raw": len(raw_items),
                "count_deduped_cross_source": len(merged),
                "count_quality_pass": sum(1 for x in merged if x.get("data_quality") == "pass"),
                "count_quality_fail": sum(1 for x in merged if x.get("data_quality") != "pass"),
                "listings": merged,
            },
            f,
            indent=2,
        )

    notion_msg = "Notion sync skipped (--no-notion)"
    if not no_notion:
        if not db_id:
            notion_msg = "Notion sync skipped: set NOTION_DATABASE_ID or pass --notion-db-id"
        else:
            try:
                notion_msg = sync_to_notion(merged, db_id)
            except Exception as e:
                notion_msg = f"Notion sync failed: {e}"

    print(f"Raw extracted: {len(raw_items)}")
    print(f"Deduped cross-source: {len(merged)}")
    print(f"Saved: {os.path.abspath(OUT_PATH)}")
    print(notion_msg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-items", type=int, default=150)
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--notion-db-id", default=os.getenv("NOTION_DATABASE_ID"))
    args = parser.parse_args()
    run(max_items=args.max_items, no_notion=args.no_notion, db_id=args.notion_db_id)
