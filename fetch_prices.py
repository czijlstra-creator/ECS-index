#!/usr/bin/env python3
"""
ECS H100-EU Index -- dagelijkse prijsfetcher
Haalt GPU-prijzen op bij Gcore (L40S), OVHcloud (H100), Hetzner (H100 NVL)
en Scaleway (H100 SXM) en schrijft ze naar prices_history.csv en prices_latest.json.
"""

import csv
import json
import os
import requests
from datetime import date, datetime, timezone


# -- Configuratie -------------------------------------------------------

GCORE_PROJECT_ID = 1186222
GCORE_REGION_ID = 76  # Luxembourg-2
GCORE_FLAVOR = "bm3-infrastructure-ai-large-l40s-48-8"
GCORE_GPU_COUNT = 8  # 8x NVIDIA L40S per server
GCORE_GPU_MODEL = "L40S"
GCORE_REGION = "Luxembourg-2 (EU)"

OVH_SUBSIDIARY = "FR"  # EUR-prijzen
OVH_REGION = "EU (FR)"

OVH_H100_PLANS = {
    "h100-380": (4, "H100 SXM 4-GPU bare metal"),
    "h100-760": (8, "H100 SXM 8-GPU bare metal"),
    "h100-1520": (16, "H100 SXM 16-GPU bare metal"),
}

HETZNER_LOCATION = "fsn1"  # Falkenstein, Germany (EU)
HETZNER_REGION = "EU (fsn1, DE)"
HETZNER_GPU_MODEL = "H100 NVL"

SCALEWAY_ZONES = ["nl-ams-1", "fr-par-2"]
SCALEWAY_REGION = "EU (AMS/PAR)"
SCALEWAY_GPU_MODEL = "H100 SXM"

CSV_FILE = "prices_history.csv"
JSON_FILE = "prices_latest.json"
CSV_FIELDS = [
    "date", "provider", "gpu_model", "region", "instance_type",
    "gpu_count", "price_per_hour_eur", "price_per_gpu_hour_eur", "note",
]


# -- Gcore -------------------------------------------------------

def fetch_gcore_prices(api_key: str) -> list[dict]:
    url = (
        f"https://api.gcore.com/cloud/v1/pricing/"
        f"{GCORE_PROJECT_ID}/{GCORE_REGION_ID}/ai/clusters"
    )
    r = requests.post(
        url,
        headers={"Authorization": f"APIKey {api_key}", "Content-Type": "application/json"},
        json={"name": "price-check", "flavor": GCORE_FLAVOR, "interfaces": [{"type": "external"}]},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    price = data["per_hour"]["flavor"]
    return [{
        "provider": "Gcore",
        "gpu_model": GCORE_GPU_MODEL,
        "region": GCORE_REGION,
        "instance_type": GCORE_FLAVOR,
        "gpu_count": GCORE_GPU_COUNT,
        "price_per_hour_eur": round(price, 4),
        "price_per_gpu_hour_eur": round(price / GCORE_GPU_COUNT, 4),
        "note": "bare metal excl. extern IP (Luxembourg-2)",
    }]


# -- OVHcloud -------------------------------------------------------

def fetch_ovhcloud_prices() -> list[dict]:
    url = (
        "https://www.ovh.com/engine/apiv6/order/catalog/public/cloud"
        f"?ovhSubsidiary={OVH_SUBSIDIARY}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    catalog = r.json()

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
            "provider": "OVHcloud",
            "gpu_model": "H100 SXM",
            "region": OVH_REGION,
            "instance_type": key,
            "gpu_count": gpu_count,
            "price_per_hour_eur": round(price, 4),
            "price_per_gpu_hour_eur": round(price / gpu_count, 4),
            "note": label,
        })
    return records


# -- Hetzner Cloud -------------------------------------------------------

_HETZNER_GPU_COUNT: dict[str, int] = {
    "gx3-8h": 1,
    "gx3-24h": 3,
    "gx3-80h": 8,
}


def fetch_hetzner_prices(api_token: str) -> list[dict]:
    url = "https://api.hetzner.cloud/v1/server_types"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_token}"},
        params={"per_page": 50},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    records = []
    for st in data.get("server_types", []):
        if not st["name"].startswith("gx"):
            continue
        price_hourly = None
        for price in st.get("prices", []):
            if price.get("location") == HETZNER_LOCATION:
                price_hourly = float(price["price_hourly"]["net"])
                break
        if price_hourly is None:
            continue
        gpu_count = st.get("gpu_count") or _HETZNER_GPU_COUNT.get(st["name"], 0)
        if not gpu_count:
            print(f"  WAARSCHUWING: GPU count onbekend voor Hetzner {st['name']} -- overgeslagen")
            continue
        records.append({
            "provider": "Hetzner",
            "gpu_model": HETZNER_GPU_MODEL,
            "region": HETZNER_REGION,
            "instance_type": st["name"],
            "gpu_count": gpu_count,
            "price_per_hour_eur": round(price_hourly, 4),
            "price_per_gpu_hour_eur": round(price_hourly / gpu_count, 4),
            "note": st.get("description", ""),
        })
    return records


# -- Scaleway -------------------------------------------------------

def _scaleway_gpu_count_from_name(name: str) -> int:
    parts = name.replace("GPU-", "").split("-")
    for part in parts:
        if part.isdigit() and part not in ("80", "94", "100"):
            return int(part)
    return 0


def fetch_scaleway_prices(secret_key: str) -> list[dict]:
    records = []
    seen: set = set()
    for zone in SCALEWAY_ZONES:
        url = f"https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
        r = requests.get(url, headers={"X-Auth-Token": secret_key}, timeout=15)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        data = r.json()
        for name, info in data.get("servers", {}).items():
            if "H100" not in name.upper() or name in seen:
                continue
            seen.add(name)
            hourly = info.get("hourly_price")
            if hourly is None:
                continue
            price = float(hourly)
            gpu_count = info.get("gpu") or _scaleway_gpu_count_from_name(name) or 1
            records.append({
                "provider": "Scaleway",
                "gpu_model": SCALEWAY_GPU_MODEL,
                "region": SCALEWAY_REGION,
                "instance_type": name,
                "gpu_count": gpu_count,
                "price_per_hour_eur": round(price, 4),
                "price_per_gpu_hour_eur": round(price / gpu_count, 4),
                "note": f"zone {zone}",
            })
    return records


# -- Output -------------------------------------------------------

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
        "date": today,
        "fetched": datetime.now(timezone.utc).isoformat(),
        "prices": records,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


# -- Main -------------------------------------------------------

def main() -> None:
    today = date.today().isoformat()
    print(f"\nECS dagelijkse prijsupdate -- {today}")
    print("=" * 50)
    all_records: list[dict] = []

    gcore_key = os.environ.get("GCORE_API_KEY", "")
    if gcore_key:
        try:
            recs = fetch_gcore_prices(gcore_key)
            all_records.extend(recs)
            for rec in recs:
                print(f"  Gcore {rec['gpu_model']:6s} ({rec['region']}): "
                      f"EUR{rec['price_per_hour_eur']}/hr ({rec['gpu_count']}x GPU), "
                      f"EUR{rec['price_per_gpu_hour_eur']}/GPU/hr")
        except Exception as exc:
            print(f"  FOUT Gcore: {exc}")
    else:
        print("  GCORE_API_KEY niet ingesteld -- overgeslagen")

    try:
        recs = fetch_ovhcloud_prices()
        all_records.extend(recs)
        for rec in recs:
            print(f"  OVHcloud {rec['gpu_model']:8s} ({rec['instance_type']}): "
                  f"EUR{rec['price_per_hour_eur']}/hr, EUR{rec['price_per_gpu_hour_eur']}/GPU/hr")
    except Exception as exc:
        print(f"  FOUT OVHcloud: {exc}")

    hetzner_token = os.environ.get("HETZNER_API_TOKEN", "")
    if hetzner_token:
        try:
            recs = fetch_hetzner_prices(hetzner_token)
            all_records.extend(recs)
            for rec in recs:
                print(f"  Hetzner {rec['gpu_model']:8s} ({rec['instance_type']}): "
                      f"EUR{rec['price_per_hour_eur']}/hr ({rec['gpu_count']}x GPU), "
                      f"EUR{rec['price_per_gpu_hour_eur']}/GPU/hr")
        except Exception as exc:
            print(f"  FOUT Hetzner: {exc}")
    else:
        print("  HETZNER_API_TOKEN niet ingesteld -- overgeslagen")

    scw_secret = os.environ.get("SCW_SECRET_KEY", "")
    if scw_secret:
        try:
            recs = fetch_scaleway_prices(scw_secret)
            all_records.extend(recs)
            for rec in recs:
                print(f"  Scaleway {rec['gpu_model']:8s} ({rec['instance_type']}): "
                      f"EUR{rec['price_per_hour_eur']}/hr ({rec['gpu_count']}x GPU), "
                      f"EUR{rec['price_per_gpu_hour_eur']}/GPU/hr")
        except Exception as exc:
            print(f"  FOUT Scaleway: {exc}")
    else:
        print("  SCW_SECRET_KEY niet ingesteld -- overgeslagen")

    if all_records:
        append_to_csv(all_records, today)
        write_latest_json(all_records, today)
        print(f"\n  --> {len(all_records)} records opgeslagen in {CSV_FILE} en {JSON_FILE}")
    else:
        print("\n  FOUT: geen prijzen opgehaald -- niets opgeslagen")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
