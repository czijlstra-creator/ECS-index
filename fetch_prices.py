#!/usr/bin/env python3
"""
ECS H100-EU Index — dagelijkse prijsfetcher
Haalt GPU-prijzen op bij Gcore (L40S) en OVHcloud (H100) en schrijft ze
naar prices_history.csv en prices_latest.json.
"""

import csv
import json
import os
import requests
from datetime import date, datetime, timezone


# ── Configuratie ──────────────────────────────────────────────────────────────

GCORE_PROJECT_ID = 1186222
GCORE_REGION_ID  = 76               # Luxembourg-2
GCORE_FLAVOR     = "bm3-infrastructure-ai-large-l40s-48-8"
GCORE_GPU_COUNT  = 8                # 8x NVIDIA L40S per server
GCORE_GPU_MODEL  = "L40S"
GCORE_REGION     = "Luxembourg-2 (EU)"

OVH_SUBSIDIARY   = "FR"             # EUR-prijzen
OVH_REGION       = "EU (FR)"

# OVH H100 bare-metal plans: planCode → (gpu_count, label)
# Naamgeving: h100-{geheugen_GB}; ratio 380:760:1520 = 1:2:4 → vermoedelijk 4/8/16x H100 SXM 80GB
OVH_H100_PLANS = {
    "h100-380":  (4,  "H100 SXM 4-GPU bare metal"),
    "h100-760":  (8,  "H100 SXM 8-GPU bare metal"),
    "h100-1520": (16, "H100 SXM 16-GPU bare metal"),
}

CSV_FILE  = "prices_history.csv"
JSON_FILE = "prices_latest.json"
CSV_FIELDS = [
    "date", "provider", "gpu_model", "region", "instance_type",
    "gpu_count", "price_per_hour_eur", "price_per_gpu_hour_eur", "note",
]


# ── Gcore ─────────────────────────────────────────────────────────────────────

def fetch_gcore_prices(api_key: str) -> list[dict]:
    """
    Haalt de L40S bare-metal prijs op via:
      POST /cloud/v1/pricing/{project_id}/{region_id}/ai/clusters
    Geeft de flavor-prijs excl. externe IP terug.
    """
    url = (
        f"https://api.gcore.com/cloud/v1/pricing/"
        f"{GCORE_PROJECT_ID}/{GCORE_REGION_ID}/ai/clusters"
    )
    r = requests.post(
        url,
        headers={
            "Authorization": f"APIKey {api_key}",
            "Content-Type":  "application/json",
        },
        json={
            "name":       "price-check",
            "flavor":     GCORE_FLAVOR,
            "interfaces": [{"type": "external"}],
        },
        timeout=15,
    )
    r.raise_for_status()
    data  = r.json()
    price = data["per_hour"]["flavor"]   # EUR/uur excl. extern IP
    return [{
        "provider":               "Gcore",
        "gpu_model":              GCORE_GPU_MODEL,
        "region":                 GCORE_REGION,
        "instance_type":          GCORE_FLAVOR,
        "gpu_count":              GCORE_GPU_COUNT,
        "price_per_hour_eur":     round(price, 4),
        "price_per_gpu_hour_eur": round(price / GCORE_GPU_COUNT, 4),
        "note":                   "bare metal excl. extern IP (Luxembourg-2)",
    }]


# ── OVHcloud ──────────────────────────────────────────────────────────────────

def fetch_ovhcloud_prices() -> list[dict]:
    """
    Haalt H100 bare-metal prijzen op uit de OVHcloud publieke catalogus.
    Filtert uitsluitend .consumption plans (excl. .monthly.postpaid).
    Catalogusprijs: pricings[0].price in eenheden van 1e-8 EUR/uur.
    """
    url = (
        "https://www.ovh.com/engine/apiv6/order/catalog/public/cloud"
        f"?ovhSubsidiary={OVH_SUBSIDIARY}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    catalog = r.json()

    # Bouw price_map: planCode → EUR/uur (alleen .consumption plans)
    price_map: dict[str, float] = {}
    for plan in catalog.get("addons", []):
        code = plan.get("planCode", "")
        if not code.endswith(".consumption"):
            continue
        pricings = plan.get("pricings", [])
        if pricings:
            price_map[code] = pricings[0].get("price", 0) * 1e-8

    records = []
    for plan_code, (gpu_count, label) in OVH_H100_PLANS.items():
        key = f"{plan_code}.consumption"
        if key not in price_map:
            print(f"  WAARSCHUWING: {key} niet gevonden in OVH-catalogus")
            continue
        price = price_map[key]
        records.append({
            "provider":               "OVHcloud",
            "gpu_model":              "H100 SXM",
            "region":                 OVH_REGION,
            "instance_type":          key,
            "gpu_count":              gpu_count,
            "price_per_hour_eur":     round(price, 4),
            "price_per_gpu_hour_eur": round(price / gpu_count, 4),
            "note":                   label,
        })
    return records


# ── Output ────────────────────────────────────────────────────────────────────

def append_to_csv(records: list[dict], today: str) -> None:
    write_header = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        for rec in records:
            w.writerow({"date": today, **rec})


def write_latest_json(records: list[dict], today: str) -> None:
    payload = {
        "date":    today,
        "fetched": datetime.now(timezone.utc).isoformat(),
        "prices":  records,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    today = date.today().isoformat()
    print(f"\nECS dagelijkse prijsupdate — {today}")
    print("=" * 50)

    all_records: list[dict] = []

    # Gcore
    gcore_key = os.environ.get("GCORE_API_KEY", "")
    if gcore_key:
        try:
            recs = fetch_gcore_prices(gcore_key)
            all_records.extend(recs)
            for rec in recs:
                print(
                    f"  Gcore  {rec['gpu_model']:6s} "
                    f"({rec['region']}): "
                    f"€{rec['price_per_hour_eur']}/hr "
                    f"({rec['gpu_count']}x GPU), "
                    f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
                )
        except Exception as exc:
            print(f"  FOUT Gcore: {exc}")
    else:
        print("  GCORE_API_KEY niet ingesteld — overgeslagen")

    # OVHcloud
    try:
        recs = fetch_ovhcloud_prices()
        all_records.extend(recs)
        for rec in recs:
            print(
                f"  OVHcloud {rec['gpu_model']:8s} "
                f"({rec['instance_type']}): "
                f"€{rec['price_per_hour_eur']}/hr, "
                f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
            )
    except Exception as exc:
        print(f"  FOUT OVHcloud: {exc}")

    if all_records:
        append_to_csv(all_records, today)
        write_latest_json(all_records, today)
        print(f"\n  → {len(all_records)} records opgeslagen in {CSV_FILE} en {JSON_FILE}")
    else:
        print("\n  FOUT: geen prijzen opgehaald — niets opgeslagen")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
