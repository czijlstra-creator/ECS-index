#!/usr/bin/env python3
"""
ECS H100-EU Index — dagelijkse prijsophaling
============================================
Haalt on-demand H100 SXM prijzen op bij drie EU-native aanbieders
en berekent de gewogen ECS-indexwaarde.

Gebruik:
    python fetch_prices.py

Vereiste omgevingsvariabelen (sla op als GitHub Secrets):
    GCORE_API_KEY
    OVH_APP_KEY
    OVH_APP_SECRET
    OVH_CONSUMER_KEY
    VERDA_CLIENT_ID
    VERDA_CLIENT_SECRET

Valutanoot:
    OVHcloud  → retourneert EUR native (ovhSubsidiary=FR)
    Gcore     → API retourneert USD; facturering aan EU-klanten in EUR.
                Zie CURRENCY_NOTE in de code.
    Verda     → API retourneert USD; EUR-billing beschikbaar via bankoverschrijving.
                Zie CURRENCY_NOTE in de code.
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timezone, date

# ── CURRENCY NOTE ──────────────────────────────────────────────────────────
# Gcore en Verda retourneren USD via hun API, ook al ondersteunen ze EUR-billing.
# Dit betekent dat hun EUR-contractprijs varieert met de wisselkoers — de koper
# draagt dus nog steeds indirect valutarisico tenzij de aanbieder een vaste EUR-prijs
# afgeeft. Dit is een openstaand strategisch punt: bij voorkeur per aanbieder nagaan
# of ze bereid zijn vaste EUR-prijs contractueel vast te leggen (bilateraal via ECS).
# Voorlopig worden hun prijzen getoond met currency = "USD (API)" zodat dit zichtbaar is.
# ─────────────────────────────────────────────────────────────────────────────

# ── INDEXGEWICHTEN ────────────────────────────────────────────────────────
WEIGHTS = {
    "OVHcloud":         0.30,
    "Gcore":            0.40,
    "DataCrunch/Verda": 0.30,
}

# Alleen aanbieders met native EUR-prijs tellen mee in de index.
# Gcore en Verda staan voorlopig op "pending" — zie currency note hierboven.
EUR_NATIVE = {
    "OVHcloud":         True,
    "Gcore":            False,   # API geeft USD — pending bevestiging vaste EUR-prijs
    "DataCrunch/Verda": False,   # API geeft USD — pending bevestiging vaste EUR-prijs
}


# ══════════════════════════════════════════════════════════════════════════
#  GCORE
# ══════════════════════════════════════════════════════════════════════════

def fetch_gcore():
    """
    Haalt H100 prijs op via Gcore Cloud API.
    Auth: APIKey header (permanent token).
    Retourneert: prijs in USD (API), instance-naam, regio.
    """
    token = os.environ["GCORE_API_KEY"]
    headers = {"Authorization": f"APIKey {token}"}

    # Stap 1: projecten ophalen
    r = requests.get(
        "https://api.gcore.com/cloud/v1/projects",
        headers=headers, timeout=15
    )
    r.raise_for_status()
    projects = r.json().get("results", [])
    if not projects:
        raise Exception("Geen Gcore projecten gevonden")
    project_id = projects[0]["id"]

    # Stap 2: regio's ophalen
    r = requests.get(
        "https://api.gcore.com/cloud/v1/regions",
        headers=headers, timeout=15
    )
    r.raise_for_status()
    regions = r.json().get("results", [])

    # Stap 3: per regio H100 flavors zoeken
    for region in regions:
        region_id = region["id"]
        r = requests.get(
            f"https://api.gcore.com/cloud/v1/flavors/{project_id}/{region_id}",
            params={"include_prices": "true"},
            headers=headers, timeout=15
        )
        if r.status_code != 200:
            continue
        for flavor in r.json().get("results", []):
            name = flavor.get("name", "")
            if "h100" not in name.lower():
                continue
            # Prijs ophalen — veld afhankelijk van API-versie
            price = (
                flavor.get("price_per_hour")
                or flavor.get("price", {}).get("price_per_hour")
            )
            if price is None:
                continue
            return {
                "provider":    "Gcore",
                "instance":    name,
                "price":       float(price),
                "api_currency":"USD",
                "region":      region.get("display_name", str(region_id)),
            }

    raise Exception("Geen H100 flavor gevonden bij Gcore")


# ══════════════════════════════════════════════════════════════════════════
#  OVHCLOUD
# ══════════════════════════════════════════════════════════════════════════

def _ovh_sign(app_secret, consumer_key, method, url, body, timestamp):
    """OVHcloud HMAC-SHA1 handtekening."""
    pre_hash = "+".join([app_secret, consumer_key, method.upper(), url, body, str(timestamp)])
    return "$1$" + hashlib.sha1(pre_hash.encode("utf-8")).hexdigest()


def _ovh_headers(method, url, body=""):
    """Bouwt de vier vereiste OVHcloud request-headers."""
    app_key      = os.environ["OVH_APP_KEY"]
    app_secret   = os.environ["OVH_APP_SECRET"]
    consumer_key = os.environ["OVH_CONSUMER_KEY"]
    timestamp    = int(datetime.now(timezone.utc).timestamp())
    return {
        "X-Ovh-Application": app_key,
        "X-Ovh-Consumer":    consumer_key,
        "X-Ovh-Timestamp":   str(timestamp),
        "X-Ovh-Signature":   _ovh_sign(app_secret, consumer_key, method, url, body, timestamp),
        "Content-Type":      "application/json",
    }


def fetch_ovh():
    """
    Haalt H100 prijs op via OVHcloud publieke catalogus API.
    ovhSubsidiary=FR geeft EUR-native prijzen terug.
    Probeert eerst de publieke catalogus (geen auth nodig),
    daarna de geauthenticeerde endpoint.
    """
    # Stap 1: publieke catalogus (geen signing nodig)
    catalog_url = "https://eu.api.ovh.com/v1/order/catalog/public/cloud?ovhSubsidiary=FR"
    r = requests.get(catalog_url, timeout=15)

    if r.status_code == 200:
        catalog = r.json()
        result = _parse_ovh_catalog(catalog)
        if result:
            return result

    # Stap 2: geauthenticeerde flavor-endpoint als fallback
    url = "https://eu.api.ovh.com/v1/cloud/subsidiaryPrice?ovhSubsidiary=FR"
    r = requests.get(url, headers=_ovh_headers("GET", url), timeout=15)
    if r.status_code == 200:
        result = _parse_ovh_catalog(r.json())
        if result:
            return result

    raise Exception("Geen H100 prijs gevonden bij OVHcloud")


def _parse_ovh_catalog(catalog):
    """Zoekt H100 GPU instance in OVHcloud catalogus en retourneert EUR-prijs."""
    OVH_PRICE_UNIT = 1_000_000_000  # OVHcloud prijzen in nano-eenheden

    plans = catalog.get("plans", [])
    for plan in plans:
        for addon in plan.get("addons", []):
            product = addon.get("product", {})
            name = product.get("name", "") or addon.get("planCode", "")
            if "h100" not in name.lower():
                continue
            for pricing in addon.get("pricings", []):
                if pricing.get("mode") in ("default", "consumption"):
                    price_raw = pricing.get("price", 0)
                    price_eur = price_raw / OVH_PRICE_UNIT
                    if price_eur > 0.01:  # filter nullen eruit
                        return {
                            "provider":    "OVHcloud",
                            "instance":    name,
                            "price":       round(price_eur, 4),
                            "api_currency":"EUR",
                            "region":      "EU (FR subsidiary)",
                        }
    return None


# ══════════════════════════════════════════════════════════════════════════
#  DATACRUNCH / VERDA
# ══════════════════════════════════════════════════════════════════════════

def fetch_verda():
    """
    Haalt H100 SXM prijs op via DataCrunch/Verda API.
    Auth: OAuth2 client_credentials flow.
    Retourneert: prijs in USD (API).
    """
    # Stap 1: access token ophalen
    token_r = requests.post(
        "https://api.verda.com/v1/oauth2/token",
        json={
            "grant_type":    "client_credentials",
            "client_id":     os.environ["VERDA_CLIENT_ID"],
            "client_secret": os.environ["VERDA_CLIENT_SECRET"],
        },
        timeout=15
    )
    token_r.raise_for_status()
    access_token = token_r.json()["access_token"]

    # Stap 2: instance types ophalen
    r = requests.get(
        "https://api.verda.com/v1/instance-types",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15
    )
    r.raise_for_status()

    # H100 SXM zoeken (bij voorkeur per GPU genormaliseerd)
    best = None
    for inst in r.json():
        name = inst.get("instance_type", "")
        if "h100" not in name.lower():
            continue
        gpu_count = inst.get("gpu", 1)
        price_total = inst.get("price_per_hour")
        if not price_total:
            continue
        price_per_gpu = round(price_total / gpu_count, 4)
        if best is None or price_per_gpu < best["price"]:
            best = {
                "provider":    "DataCrunch/Verda",
                "instance":    name,
                "price":       price_per_gpu,
                "api_currency":"USD",
                "region":      "FI",
                "gpu_count":   gpu_count,
            }

    if best:
        return best
    raise Exception("Geen H100 instance gevonden bij DataCrunch/Verda")


# ══════════════════════════════════════════════════════════════════════════
#  INDEXBEREKENING
# ══════════════════════════════════════════════════════════════════════════

def compute_index(results):
    """
    Berekent gewogen gemiddelde index op basis van EUR-native prijzen.
    Aanbieders waarvan api_currency != EUR worden gemarkeerd maar voorlopig
    meegenomen als indicatie (pending valutabevestiging).
    """
    weighted_sum  = 0.0
    weight_used   = 0.0
    for r in results:
        provider = r["provider"]
        weight   = WEIGHTS.get(provider, 0)
        weighted_sum  += r["price"] * weight
        weight_used   += weight

    if weight_used == 0:
        return None
    # Normaliseer voor het geval niet alle providers beschikbaar zijn
    return round(weighted_sum / weight_used, 4)


# ══════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    today = date.today().isoformat()
    print(f"\nECS H100-EU Index — prijsophaling {today}")
    print("=" * 55)

    fetchers = [
        ("OVHcloud",         fetch_ovh),
        ("Gcore",            fetch_gcore),
        ("DataCrunch/Verda", fetch_verda),
    ]

    results = []
    errors  = []

    for name, fn in fetchers:
        try:
            result = fn()
            results.append(result)
            currency_flag = "" if result["api_currency"] == "EUR" else f" ⚠ API retourneert {result['api_currency']}"
            print(f"✓ {result['provider']:20s}  {result['api_currency']} {result['price']:.4f}/GPU-uur  ({result['instance']}){currency_flag}")
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})
            print(f"✗ {name:20s}  FOUT: {e}")

    print()

    if results:
        index_value = compute_index(results)
        eur_only = [r for r in results if r["api_currency"] == "EUR"]
        print(f"ECS H100-EU Index (gewogen):  {index_value:.4f}")
        if len(eur_only) < len(results):
            non_eur = [r["provider"] for r in results if r["api_currency"] != "EUR"]
            print(f"⚠ Let op: {', '.join(non_eur)} retourneert USD via API.")
            print(f"  Zie CURRENCY_NOTE in de code — bevestig vaste EUR-contractprijs.")

    if errors:
        print(f"\n{len(errors)} aanbieder(s) niet bereikbaar: {[e['provider'] for e in errors]}")

    # Output als JSON voor de pipeline
    output = {
        "date":        today,
        "index":       compute_index(results) if results else None,
        "providers":   results,
        "errors":      errors,
    }
    print("\nJSON output:")
    print(json.dumps(output, indent=2))
    return output


if __name__ == "__main__":
    main()
