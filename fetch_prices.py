#!/usr/bin/env python3
# ECS Compute Index - dagelijkse prijsfetcher
# Haalt GPU-prijzen op bij Gcore (L40S), OVHcloud (H100), Hetzner (H100 NVL),
# Scaleway (H100 SXM), Verda (H100/H200/B200), Nebius (H100/H200 NVL),
# CoreWeave (H100/H200/B200 EU) en Genesis Cloud (H100/H200/B200 EU) en
# schrijft ze naar prices_history.csv en prices_latest.json.
import csv
import json
import os
import requests
from datetime import date, datetime, timezone


# ── Configuratie ──────────────────────────────────────────────────────────────

GCORE_PROJECT_ID = 1186222
GCORE_REGION_ID = 76  # Luxembourg-2
GCORE_REGION = "Luxembourg-2 (EU)"

# Gcore bare-metal AI flavors — EUR-native via Gcore cloud-API
# (gpu_model, flavor_slug, gpu_count_per_node, sanity_range_eur)
_GCORE_GPUS = [
    ("L40S", "bm3-infrastructure-ai-large-l40s-48-8", 8, (0.80, 3.00)),
    ("H100", "bm3-ai-1xlarge-h100-80-8",              8, (1.50, 5.00)),
    ("H200", "bm3-ai-1xlarge-h200-141-8",             8, (2.00, 6.00)),
]

OVH_SUBSIDIARY = "FR"  # EUR-prijzen
OVH_REGION = "EU (FR)"

# OVH H100 bare-metal plans: planCode → (gpu_count, label)
# Naamgeving: h100-{geheugen_GB}; ratio 380:760:1520 = 1:2:4 → vermoedelijk 4/8/16x H100 SXM 80GB
OVH_H100_PLANS = {
    "h100-380": (4, "H100 SXM 4-GPU bare metal"),
    "h100-760": (8, "H100 SXM 8-GPU bare metal"),
    "h100-1520": (16, "H100 SXM 16-GPU bare metal"),
}

HETZNER_LOCATION = "fsn1"  # Falkenstein, Germany (EU)
HETZNER_REGION = "EU (fsn1, DE)"
HETZNER_GPU_MODEL = "H100 NVL"

SCALEWAY_ZONES = ["nl-ams-1", "fr-par-2"]  # EU zones met GPU-aanbod
SCALEWAY_REGION = "EU (AMS/PAR)"
SCALEWAY_GPU_MODEL = "H100 SXM"

VERDA_BASE_URL = "https://api.datacrunch.io/v1"
VERDA_REGION = "EU (FI/IS)"  # Helsinki (FI) + Reykjanesbær (IS)

# Welke GPU-families meenemen + sanity-range (EUR/GPU/hr).
# Matching gebeurt case-insensitive via 'family in model'. Voeg hier een
# regel toe als Verda een nieuwe Blackwell/Hopper SKU lanceert.
_VERDA_GPUS = [
    ("H100", (1.00, 5.00)),
    ("H200", (1.50, 6.00)),
    ("B200", (2.00, 8.00)),
]

# Nebius (public docs parsing - geen API-key nodig)
# B200 bewust uitgesloten: Nebius biedt B200 alleen in us-central1 en me-west1,
# geen EU-regio. Valt buiten de EU-scope van ECS.
# Sinds 1 oktober 2025 toont de pricing-pagina zowel de oude split-tabel
# (GPU/CPU/RAM apart) als de nieuwe unified-tabel. De regex gebruikt een
# negatieve lookahead op ". GPU"/". CPU"/". RAM" om alleen de unified-rij
# te matchen, niet de oude split-waarden.
NEBIUS_PRICING_URL = "https://docs.nebius.com/compute/resources/pricing/"
FRANKFURTER_URL = "https://api.frankfurter.app/latest"

_NEBIUS_GPUS = [
    ("H100 NVL",
     r"(?<!Preemptible\s)NVIDIA[^$<]*?H100 NVLink with Intel Sapphire Rapids"
     r"(?!\.\s*(?:GPU|CPU|RAM))[^$<]*?\$(\d+\.\d+)[^1]*?1\s*GPU\s*hour",
     "EU (FI, eu-north1)", (1.50, 5.00)),
    ("H200 NVL",
     r"(?<!Preemptible\s)NVIDIA[^$<]*?H200 NVLink with Intel Sapphire Rapids"
     r"(?!\.\s*(?:GPU|CPU|RAM))[^$<]*?\$(\d+\.\d+)[^1]*?1\s*GPU\s*hour",
     "EU (FI/FR, eu-north1/eu-west1)", (2.00, 6.00)),
]

# CoreWeave (public pricing-pagina; EU-sectie)
COREWEAVE_PRICING_URL = "https://www.coreweave.com/pricing"
COREWEAVE_REGION = "EU (NL/NO)"

_COREWEAVE_GPUS = [
    # (label, regex binnen EU-sectie, gpu_count per node, sanity range USD)
    ("H100", r"NVIDIA HGX H100 On-Demand Price:\s*\$(\d+\.\d+)",  8, (30.00, 80.00)),
    ("H200", r"NVIDIA HGX H200 On-Demand Price:\s*\$(\d+\.\d+)",  8, (30.00, 90.00)),
    ("B200", r"NVIDIA HGX B200 On-Demand Price:\s*\$(\d+\.\d+)",  8, (40.00, 120.00)),
]

# Genesis Cloud (per-GPU product-pagina's; on-demand tarief)
GENESIS_PRODUCT_PAGES = {
    "H100": "https://www.genesiscloud.com/products/nvidia-hgx-h100",
    "H200": "https://www.genesiscloud.com/products/nvidia-hgx-h200",
    "B200": "https://www.genesiscloud.com/products/nvidia-hgx-b200",
}
GENESIS_REGION_MAP = {
    "H100": "EU (NO/FR/ES/FI)",
    "H200": "EU (FR/ES/FI)",
    "B200": "EU (NO)",
}

# Per-GPU regex overrides voor Genesis Cloud productpagina's
# Default patroon pakt H100 en B200; H200 heeft "Available" tussen /h en On-Demand
_GENESIS_PATTERNS = {
    "default": r"\$\s*(\d+\.\d+)\s*/h\s+On-demand",
    "H200":    r"\$\s*(\d+\.\d+)\s*/h\s+Available\s+On-Demand",
}

_GENESIS_SANITY = {
    "H100": (1.50, 5.00),
    "H200": (2.00, 6.00),
    "B200": (2.50, 8.00),
}

CSV_FILE = "prices_history.csv"
JSON_FILE = "prices_latest.json"
CSV_FIELDS = [
    "date", "provider", "gpu_model", "region", "instance_type",
    "gpu_count", "price_per_hour_eur", "price_per_gpu_hour_eur", "note",
]


# ── Gcore ─────────────────────────────────────────────────────────────────────
def fetch_gcore_prices(api_key: str) -> list[dict]:
    results = []
    headers = {"Authorization": f"APIKey {api_key}"}

    # Stap 1: alle regio's ophalen
    regions_url = "https://api.gcore.com/cloud/v1/regions"
    try:
        rr = requests.get(regions_url, headers=headers, timeout=30)
        rr.raise_for_status()
        regions_data = rr.json()
    except Exception as e:
        print(f"  WAARSCHUWING: Gcore regions-endpoint fout: {e}")
        regions = [{"id": GCORE_REGION_ID, "display_name": GCORE_REGION}]
    else:
        regions_items = (
            regions_data.get("results") if isinstance(regions_data, dict) else regions_data
        )
        regions = [
            {"id": r.get("id"), "display_name": r.get("display_name") or r.get("name")}
            for r in (regions_items or [])
            if isinstance(r, dict) and r.get("id") is not None
        ] or [{"id": GCORE_REGION_ID, "display_name": GCORE_REGION}]

    print(f"  [Gcore] {len(regions)} regio's aangetroffen")

    # Stap 2: per regio bmflavors + prijzen ophalen
    all_flavors = []
    for region in regions:
        region_id = region["id"]
        region_name = region["display_name"]
        url = (
            f"https://api.gcore.com/cloud/v1/bmflavors/"
            f"{GCORE_PROJECT_ID}/{region_id}?include_prices=true"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code in (403, 404):
                continue  # project heeft geen toegang tot deze regio
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  WAARSCHUWING: Gcore regio {region_name}: {e}")
            continue
        items = data.get("results") if isinstance(data, dict) else data
        for it in (items or []):
            if isinstance(it, dict):
                it["_region_name"] = region_name
                all_flavors.append(it)

    # Diagnostic: welke GPU-flavors zijn in totaal zichtbaar?
    gpu_names = sorted({
        f.get("flavor_name") for f in all_flavors
        if f.get("flavor_name") and any(
            g in f["flavor_name"].lower()
            for g in ("h100", "h200", "l40s", "b200", "gb200")
        )
    })
    print(f"  [Gcore] GPU-flavors over alle regio's: {gpu_names}")

    # Stap 3: matchen tegen onze config
    for gpu_model, flavor_slug, gpu_count, (lo, hi) in _GCORE_GPUS:
        item = next(
            (f for f in all_flavors if f.get("flavor_name") == flavor_slug),
            None,
        )
        if not item:
            print(f"  WAARSCHUWING: Gcore flavor niet gevonden: {flavor_slug}")
            continue

        price_raw = item.get("price_per_hour")
        if isinstance(price_raw, dict):
            price_raw = price_raw.get("value") or price_raw.get("amount")

        try:
            price_per_node_hour = float(price_raw) if price_raw is not None else 0.0
        except (TypeError, ValueError):
            print(f"  WAARSCHUWING: Gcore prijs niet parsebaar voor {flavor_slug}: {price_raw}")
            continue

        if price_per_node_hour == 0.0:
            print(f"  WAARSCHUWING: Gcore prijs 0.00 voor {flavor_slug}")
            continue

        price_per_gpu_hour = price_per_node_hour / gpu_count
        if not (lo <= price_per_gpu_hour <= hi):
            print(
                f"  WAARSCHUWING: Gcore {gpu_model} €{price_per_gpu_hour:.4f}/GPU/hr "
                f"buiten sanity-range {lo}-{hi} - overgeslagen"
            )
            continue

        results.append({
            "provider": "Gcore",
            "gpu_model": gpu_model,
            "region": item.get("_region_name", GCORE_REGION),
            "instance_type": flavor_slug,
            "gpu_count": gpu_count,
            "price_per_hour_eur": round(price_per_node_hour, 4),
            "price_per_gpu_hour_eur": round(price_per_gpu_hour, 4),
            "note": "Gcore bare-metal cloud-API (multi-region)",
        })
    return results

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

    # Bouw price_map: planCode -> EUR/uur (alleen .consumption plans)
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


# ── Hetzner Cloud ─────────────────────────────────────────────────────────────

# Fallback GPU-count tabel voor Hetzner servertypen (als API geen gpu_count geeft)
_HETZNER_GPU_COUNT: dict[str, int] = {
    "gx3-8h": 1,
    "gx3-24h": 3,
    "gx3-80h": 8,
}


def fetch_hetzner_prices(api_token: str) -> list[dict]:
    """
    Haalt GPU-serverprijzen op via de Hetzner Cloud API.
    GET /v1/server_types - filtert servers met naam-prefix 'gx'.
    Prijs: EUR/uur (net, ex-BTW) voor locatie fsn1 (Falkenstein, DE).
    """
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
        # Zoek EUR/uur (net, ex-BTW) voor locatie fsn1
        price_hourly = None
        for price in st.get("prices", []):
            if price.get("location") == HETZNER_LOCATION:
                price_hourly = float(price["price_hourly"]["net"])
                break
        if price_hourly is None:
            continue
        # GPU count: uit API-veld of uit fallback tabel
        gpu_count = (
            st.get("gpu_count")
            or _HETZNER_GPU_COUNT.get(st["name"], 0)
        )
        if not gpu_count:
            print(f"  WAARSCHUWING: GPU count onbekend voor Hetzner {st['name']} - overgeslagen")
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


# ── Scaleway ──────────────────────────────────────────────────────────────────

def _scaleway_gpu_count_from_name(name: str) -> int:
    """Leid GPU-count af uit instantienaam (bv. 'H100-4-80G' -> 4, 'GPU-H100-SXM-2' -> 2)."""
    parts = name.replace("GPU-", "").split("-")
    for part in parts:
        if part.isdigit() and part not in ("80", "94", "100"):
            return int(part)
    return 0


def fetch_scaleway_prices(secret_key: str) -> list[dict]:
    """
    Haalt H100-instantieprijzen op via de Scaleway Instance API.
    GET /instance/v1/zones/{zone}/products/servers
    Filtert instanties met 'H100' in de naam.
    Prijs: EUR/uur (hourly_price).
    """
    records = []
    seen: set = set()

    for zone in SCALEWAY_ZONES:
        url = f"https://api.scaleway.com/instance/v1/zones/{zone}/products/servers"
        r = requests.get(
            url,
            headers={"X-Auth-Token": secret_key},
            timeout=15,
        )
        if r.status_code == 404:
            continue
        r.raise_for_status()
        data = r.json()

        for name, info in data.get("servers", {}).items():
            if "H100" not in name.upper():
                continue
            if name in seen:
                continue
            seen.add(name)

            hourly = info.get("hourly_price")
            if hourly is None:
                continue
            price = float(hourly)
            gpu_count = info.get("gpu") or _scaleway_gpu_count_from_name(name)
            if not gpu_count:
                gpu_count = 1  # conservatieve fallback

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


# ── Verda (formerly DataCrunch) ───────────────────────────────────────────────

def fetch_verda_prices(client_id: str, client_secret: str) -> list[dict]:
    """
    Haalt on-demand GPU-instantieprijzen op via de Verda (DataCrunch) Public API.
    Stap 1: OAuth2 client-credentials token ophalen.
    Stap 2: GET /v1/instance-types?currency=EUR; per record bepalen we in welke
    _VERDA_GPUS-familie het GPU-model valt. Buiten _VERDA_GPUS → overgeslagen.
    Sanity-range geldt op price_per_gpu_hour_eur.
    """
    # Stap 1: token ophalen
    token_r = requests.post(
        f"{VERDA_BASE_URL}/oauth2/token",
        json={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    token_r.raise_for_status()
    access_token = token_r.json()["access_token"]

    # Stap 2: instantietypen ophalen
    types_r = requests.get(
        f"{VERDA_BASE_URL}/instance-types",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"currency": "eur"},
        timeout=15,
    )
    types_r.raise_for_status()
    instance_types = types_r.json()

    sanity = dict(_VERDA_GPUS)
    records = []
    for it in instance_types:
        model_raw = (it.get("model") or "").upper()
        family = next((f for f, _ in _VERDA_GPUS if f in model_raw), None)
        if family is None:
            continue
        lo, hi = sanity[family]

        price_str = it.get("price_per_hour")
        if not price_str:
            continue
        price = float(price_str)
        gpu_count = (it.get("gpu") or {}).get("number_of_gpus") or 1
        price_per_gpu = price / gpu_count
        if not (lo <= price_per_gpu <= hi):
            print(
                f"  WAARSCHUWING: Verda {family} ({it.get('instance_type')}) "
                f"€{price_per_gpu:.4f}/GPU/hr buiten sanity-range — overgeslagen"
            )
            continue
        records.append({
            "provider": "Verda",
            "gpu_model": it.get("model", family),
            "region": VERDA_REGION,
            "instance_type": it.get("instance_type", ""),
            "gpu_count": gpu_count,
            "price_per_hour_eur": round(price, 4),
            "price_per_gpu_hour_eur": round(price_per_gpu, 4),
            "note": (it.get("gpu") or {}).get("description", ""),
        })
    return records

# ── Shared helper: USD → EUR via ECB dagreferentie ───────────────────────────

def _usd_to_eur_rate() -> float:
    r = requests.get(f"{FRANKFURTER_URL}?from=USD&to=EUR", timeout=10)
    r.raise_for_status()
    rate = r.json()["rates"]["EUR"]
    if not (0.70 < rate < 1.30):
        raise ValueError(f"ECB USD->EUR koers buiten sanity-range: {rate}")
    return rate


# ── Nebius (docs-parsing) ────────────────────────────────────────────────────

def fetch_nebius_prices() -> list[dict]:
    """
    Parseert de officiële Nebius pricing docs-pagina.
    Elke dagelijkse run haalt de actuele rate card op — prijswijzigingen
    bij Nebius rollen binnen 6 uur door naar de index.
    """
    import re
    r = requests.get(NEBIUS_PRICING_URL, timeout=20)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"\s+", " ", text)
    fx = _usd_to_eur_rate()
    records = []
    for gpu_label, pattern, region, (lo, hi) in _NEBIUS_GPUS:
        m = re.search(pattern, text)
        if not m:
            print(f"  WAARSCHUWING: Nebius {gpu_label} niet gevonden in docs — layout gewijzigd?")
            continue
        price_usd = float(m.group(1))
        if not (lo <= price_usd <= hi):
            print(f"  WAARSCHUWING: Nebius {gpu_label} prijs ${price_usd} buiten sanity-range — overgeslagen")
            continue
        price_eur = price_usd * fx
        records.append({
            "provider": "Nebius",
            "gpu_model": gpu_label,
            "region": region,
            "instance_type": f"{gpu_label.lower().replace(' ', '-')}-1gpu",
            "gpu_count": 1,
            "price_per_hour_eur": round(price_eur, 4),
            "price_per_gpu_hour_eur": round(price_eur, 4),
            "note": f"on-demand, ${price_usd}/GPU/hr x ECB {fx:.4f}",
        })
    return records


# ── CoreWeave (docs-parsing; EU-regio) ───────────────────────────────────────

def fetch_coreweave_prices() -> list[dict]:
    """
    Parseert CoreWeave's officiële pricing-pagina. Isoleert eerst de
    'REGION: EUROPE' sectie en zoekt daarbinnen de H100/H200/B200 node-prijzen.
    Price quotes zijn voor de 8-GPU node — afgeleid per GPU-hour.
    """
    import re
    r = requests.get(COREWEAVE_PRICING_URL, timeout=20)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"\s+", " ", text)

    eu_match = re.search(
        r"REGION:\s*EUROPE(.*?)(?:On-demand CPU instances|Reserved capacity)",
        text, re.IGNORECASE,
    )
    if not eu_match:
        print("  WAARSCHUWING: CoreWeave EU-sectie niet gevonden — layout gewijzigd?")
        return []
    eu_text = eu_match.group(1)

    fx = _usd_to_eur_rate()
    records = []
    for gpu_label, pattern, gpu_count, (lo, hi) in _COREWEAVE_GPUS:
        m = re.search(pattern, eu_text)
        if not m:
            print(f"  WAARSCHUWING: CoreWeave {gpu_label} EU-prijs niet gevonden — layout gewijzigd?")
            continue
        node_usd = float(m.group(1))
        if not (lo <= node_usd <= hi):
            print(f"  WAARSCHUWING: CoreWeave {gpu_label} nodeprijs ${node_usd} buiten sanity-range — overgeslagen")
            continue
        node_eur = node_usd * fx
        records.append({
            "provider": "CoreWeave",
            "gpu_model": gpu_label,
            "region": COREWEAVE_REGION,
            "instance_type": f"hgx-{gpu_label.lower()}-{gpu_count}gpu",
            "gpu_count": gpu_count,
            "price_per_hour_eur": round(node_eur, 4),
            "price_per_gpu_hour_eur": round(node_eur / gpu_count, 4),
            "note": f"on-demand 8-GPU node, ${node_usd}/node x ECB {fx:.4f}",
        })
    return records


# ── Genesis Cloud (product-page parsing; on-demand per GPU) ──────────────────

def fetch_genesiscloud_prices() -> list[dict]:
    """
    Parseert de on-demand prijs per GPU op elke Genesis Cloud product-pagina
    (H100 / H200 / B200). Pattern wordt per GPU gekozen via _GENESIS_PATTERNS,
    omdat de H200-pagina een afwijkende hero-opmaak heeft.
    EU-datacenters per GPU zijn vastgelegd in GENESIS_REGION_MAP.
    """
    import re
    fx = _usd_to_eur_rate()
    records = []
    for gpu_label, url in GENESIS_PRODUCT_PAGES.items():
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
        except Exception as exc:
            print(f"  WAARSCHUWING: Genesis Cloud {gpu_label} pagina niet bereikbaar: {exc}")
            continue
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        pattern = _GENESIS_PATTERNS.get(gpu_label, _GENESIS_PATTERNS["default"])
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            print(f"  WAARSCHUWING: Genesis Cloud {gpu_label} on-demand prijs niet gevonden - layout gewijzigd?")
            continue
        price_usd = float(m.group(1))
        lo, hi = _GENESIS_SANITY[gpu_label]
        if not (lo <= price_usd <= hi):
            print(f"  WAARSCHUWING: Genesis Cloud {gpu_label} prijs ${price_usd} buiten sanity-range - overgeslagen")
            continue
        price_eur = price_usd * fx
        records.append({
            "provider": "Genesis Cloud",
            "gpu_model": gpu_label,
            "region": GENESIS_REGION_MAP[gpu_label],
            "instance_type": f"hgx-{gpu_label.lower()}-1gpu",
            "gpu_count": 1,
            "price_per_hour_eur": round(price_eur, 4),
            "price_per_gpu_hour_eur": round(price_eur, 4),
            "note": f"on-demand per GPU, ${price_usd}/GPU/hr x ECB {fx:.4f}",
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
        "date": today,
        "fetched": datetime.now(timezone.utc).isoformat(),
        "prices": records,
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
                    f"  Gcore {rec['gpu_model']:6s} "
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

    # Hetzner
    hetzner_token = os.environ.get("HETZNER_API_TOKEN", "")
    if hetzner_token:
        try:
            recs = fetch_hetzner_prices(hetzner_token)
            all_records.extend(recs)
            for rec in recs:
                print(
                    f"  Hetzner {rec['gpu_model']:8s} "
                    f"({rec['instance_type']}): "
                    f"€{rec['price_per_hour_eur']}/hr "
                    f"({rec['gpu_count']}x GPU), "
                    f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
                )
        except Exception as exc:
            print(f"  FOUT Hetzner: {exc}")
    else:
        print("  HETZNER_API_TOKEN niet ingesteld — overgeslagen")

    # Scaleway
    scw_secret = os.environ.get("SCW_SECRET_KEY", "")
    if scw_secret:
        try:
            recs = fetch_scaleway_prices(scw_secret)
            all_records.extend(recs)
            for rec in recs:
                print(
                    f"  Scaleway {rec['gpu_model']:8s} "
                    f"({rec['instance_type']}): "
                    f"€{rec['price_per_hour_eur']}/hr "
                    f"({rec['gpu_count']}x GPU), "
                    f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
                )
        except Exception as exc:
            print(f"  FOUT Scaleway: {exc}")
    else:
        print("  SCW_SECRET_KEY niet ingesteld — overgeslagen")

    # Verda (formerly DataCrunch)
    verda_client_id = os.environ.get("VERDA_CLIENT_ID", "")
    verda_client_secret = os.environ.get("VERDA_CLIENT_SECRET", "")
    if verda_client_id and verda_client_secret:
        try:
            recs = fetch_verda_prices(verda_client_id, verda_client_secret)
            all_records.extend(recs)
            for rec in recs:
                print(
                    f"  Verda {rec['gpu_model']:8s} "
                    f"({rec['instance_type']}): "
                    f"€{rec['price_per_hour_eur']}/hr "
                    f"({rec['gpu_count']}x GPU), "
                    f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
                )
        except Exception as exc:
            print(f"  FOUT Verda: {exc}")
    else:
        print("  VERDA_CLIENT_ID/SECRET niet ingesteld — overgeslagen")

# Nebius (public docs parsing — geen API-key)
    try:
        recs = fetch_nebius_prices()
        all_records.extend(recs)
        for rec in recs:
            print(
                f"  Nebius {rec['gpu_model']:10s} "
                f"({rec['region']}): "
                f"€{rec['price_per_hour_eur']}/hr "
                f"({rec['gpu_count']}x GPU), "
                f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
            )
    except Exception as exc:
        print(f"  FOUT Nebius: {exc}")

    # CoreWeave (public docs parsing — geen API-key)
    try:
        recs = fetch_coreweave_prices()
        all_records.extend(recs)
        for rec in recs:
            print(
                f"  CoreWeave {rec['gpu_model']:6s} "
                f"({rec['region']}): "
                f"€{rec['price_per_hour_eur']}/hr "
                f"({rec['gpu_count']}x GPU), "
                f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
            )
    except Exception as exc:
        print(f"  FOUT CoreWeave: {exc}")

    # Genesis Cloud (public product-page parsing — geen API-key)
    try:
        recs = fetch_genesiscloud_prices()
        all_records.extend(recs)
        for rec in recs:
            print(
                f"  Genesis Cloud {rec['gpu_model']:6s} "
                f"({rec['region']}): "
                f"€{rec['price_per_hour_eur']}/hr "
                f"({rec['gpu_count']}x GPU), "
                f"€{rec['price_per_gpu_hour_eur']}/GPU/hr"
            )
    except Exception as exc:
        print(f"  FOUT Genesis Cloud: {exc}")
    
    if all_records:
        append_to_csv(all_records, today)
        write_latest_json(all_records, today)
        print(f"\n  → {len(all_records)} records opgeslagen in {CSV_FILE} en {JSON_FILE}")
    else:
        print("\n  FOUT: geen prijzen opgehaald — niets opgeslagen")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
