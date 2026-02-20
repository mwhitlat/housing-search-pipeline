#!/usr/bin/env python3
import json
import os
import urllib.request
from urllib.error import HTTPError

DB_ID = "30dd08d008dd80b1a614d874a5db8468"
API_VERSION = "2022-06-28"


def req(method: str, path: str, token: str, payload=None):
    r = urllib.request.Request(
        f"https://api.notion.com{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": API_VERSION,
            "Content-Type": "application/json",
        },
        data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {body[:400]}")


def rt_text(prop):
    arr = (prop or {}).get("rich_text", [])
    return "\n".join([x.get("plain_text", "") for x in arr]).strip()


def ms_names(prop):
    return [x.get("name") for x in (prop or {}).get("multi_select", []) if x.get("name")]


def get_url(prop):
    return (prop or {}).get("url") or ""


def maybe_rename_legacy_columns(token: str):
    db = req("GET", f"/v1/databases/{DB_ID}", token)
    props = db.get("properties", {})

    rename_map = {
        "URL": "LEGACY URL",
        "Listing ID": "LEGACY Listing ID",
        "Source": "LEGACY Source",
    }

    patch = {old: {"name": new} for old, new in rename_map.items() if old in props and new not in props}
    if patch:
        req("PATCH", f"/v1/databases/{DB_ID}", token, {"properties": patch})
        return f"Renamed legacy columns: {', '.join(patch.keys())}"
    return "No legacy column renames needed"


def main():
    token = os.getenv("NOTION_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("Missing NOTION_API_TOKEN")

    # migrate page values into consolidated fields
    updated = 0
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        res = req("POST", f"/v1/databases/{DB_ID}/query", token, payload)

        for page in res.get("results", []):
            p = page.get("properties", {})

            new_listing_ids = rt_text(p.get("Listing IDs"))
            old_listing_id = rt_text(p.get("Listing ID"))

            new_source_urls = rt_text(p.get("Source URLs"))
            old_url = get_url(p.get("URL"))
            primary_url = get_url(p.get("Primary URL"))

            new_sources = set(ms_names(p.get("Sources")))
            old_source_select = ((p.get("Source") or {}).get("select") or {}).get("name")

            changed = {}

            if old_listing_id and old_listing_id not in new_listing_ids:
                merged = (new_listing_ids + "\n" + old_listing_id).strip() if new_listing_ids else old_listing_id
                changed["Listing IDs"] = {"rich_text": [{"text": {"content": merged[:1800]}}]}

            urls = [u for u in [new_source_urls, old_url] if u]
            if urls:
                merged_urls = "\n".join(dict.fromkeys("\n".join(urls).split("\n")))
                if merged_urls != new_source_urls:
                    changed["Source URLs"] = {"rich_text": [{"text": {"content": merged_urls[:1800]}}]}

            if not primary_url and old_url:
                changed["Primary URL"] = {"url": old_url}

            if old_source_select and old_source_select not in new_sources:
                new_sources.add(old_source_select)
                changed["Sources"] = {"multi_select": [{"name": s} for s in sorted(new_sources)]}

            if changed:
                req("PATCH", f"/v1/pages/{page['id']}", token, {"properties": changed})
                updated += 1

        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")

    rename_msg = maybe_rename_legacy_columns(token)
    print(f"Updated pages: {updated}")
    print(rename_msg)


if __name__ == "__main__":
    main()
