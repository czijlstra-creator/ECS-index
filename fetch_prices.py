#!/usr/bin/env python3
"""
ECS H100-EU Index — debug / discovery script
Drukt ruwe API-responses af zodat we de juiste endpoints kunnen vinden.
"""

import os
import json
import hashlib
import requests
from datetime import datetime, timezone, date


# ══════════════════════════════════════════════════════════════════════════
#  GCORE — discovery
# ══════════════════════════════════════════════════════════════════════════

def debug_gcore():
    print("\n── GCORE ─────────────────────────────────────")
    token = os.environ.get("GCORE_API_KEY", "")
    if not token:
        print("FOUT: GCORE_API_KEY niet ingesteld")
        return

    # Probeer beide auth-formaten
    for auth_format in [f"APIKey {token}", f"Bearer {token}"]:
        headers = {"Authorization": auth_format}
        print(f"\nAuth-formaat: {auth_format[:30]}...")

        # Test 1: projecten
        r = requests.get("https://api.gcore.com/cloud/v1/projects",
                         headers=headers, timeout=15)
        print(f"  /projects → status {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Response: {json.dumps(data)[:300]}")
            # Regio's ophalen
            r_reg = requests.get("https://api.gcore.com/cloud/v1/regions", headers=headers, timeout=15)
            all_regions = r_reg.json().get("results", [{"id": 76}, {"id": 9}]) if r_reg.status_code == 200 else [{"id": 76}, {"id": 9}]
            print(f"  Regio's: {[(r['id'], r.get('display_name','?')) for r in all_regions]}")
            # Probeer flavors voor elk project
            for proj in data.get("results", [])[:2]:
                pid = proj["id"]
                for reg in all_regions:
                    rid = reg.get("id")
                    r2 = requests.get(
                        f"https://api.gcore.com/cloud/v1/flavors/{pid}/{rid}",
                        headers=headers, timeout=15)
                    if r2.status_code == 200:
                        flavors = r2.json().get("results", [])
                        gpu = [f["name"] for f in flavors if any(k in f.get("name","").lower() for k in ["gpu","h100","a100"])]
                        if gpu:
                            print(f"  Regio {rid}: GPU: {gpu[:5]}")
                        else:
                            print(f"  Regio {rid}: geen GPU ({len(flavors)} flavors)")
                    else:
                        print(f"  Regio {rid}: {r2.status_code}")
            break
        else:
            print(f"  Response: {r.text[:200]}")


# ══════════════════════════════════════════════════════════════════════════
#  OVHCLOUD — discovery
# ══════════════════════════════════════════════════════════════════════════

def debug_ovh():
    print("\n── OVHCLOUD ──────────────────────────────────")

    # Test publieke catalogus
    url = "https://eu.api.ovh.com/v1/order/catalog/public/cloud?ovhSubsidiary=FR"
    r = requests.get(url, timeout=15)
    print(f"Publieke catalogus → status {r.status_code}")

    if r.status_code == 200:
        catalog = r.json()
        plans = catalog.get("plans", [])
        print(f"Aantal plans: {len(plans)}")

        # Zoek op gpu / h100 / a100 in plan codes en namen
        gpu_items = []
        for plan in plans:
            code = plan.get("planCode", "")
            name = plan.get("invoiceName", "")
            if any(k in (code + name).lower() for k in ["gpu", "h100", "a100", "b3", "t2"]):
                gpu_items.append({"planCode": code, "name": name})
            for addon in plan.get("addons", []):
                acode = addon.get("planCode", "")
                aname = addon.get("product", {}).get("name", "")
                if any(k in (acode + aname).lower() for k in ["gpu", "h100", "a100"]):
                    gpu_items.append({"planCode": acode, "name": aname})

        for a in catalog.get("addons", []):
            code = a.get("planCode", "")
            name = a.get("invoiceName", "")
            if any(k in (code + name).lower() for k in ["gpu", "h100", "a100"]):
                gpu_items.append({"planCode": code, "name": name})
        print(f"GPU-gerelateerde items ({len(gpu_items)}):")
        for item in gpu_items[:20]:
            print(f"  {item}")
    else:
        print(f"Response: {r.text[:300]}")


# ══════════════════════════════════════════════════════════════════════════
#  DATACRUNCH / VERDA — discovery
# ══════════════════════════════════════════════════════════════════════════

def debug_verda():
    print("\n── DATACRUNCH / VERDA ────────────────────────")
    client_id     = os.environ.get("VERDA_CLIENT_ID", "")
    client_secret = os.environ.get("VERDA_CLIENT_SECRET", "")

    if not client_id:
        print("FOUT: VERDA_CLIENT_ID niet ingesteld")
        return

    print(f"Client ID (eerste 8 tekens): {client_id[:8]}...")

    # Test token endpoint
    r = requests.post(
        "https://api.verda.com/v1/oauth2/token",
        json={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        timeout=15
    )
    print(f"Token endpoint (verda.com) → status {r.status_code}")
    if r.status_code == 200:
        token = r.json()["access_token"]
        print("  Token opgehaald ✓")
        # Instance types ophalen
        r2 = requests.get(
            "https://api.verda.com/v1/instance-types",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        print(f"  /instance-types → status {r2.status_code}")
        if r2.status_code == 200:
            instances = r2.json()
            gpu = [i for i in instances if "h100" in i.get("instance_type","").lower() or "a100" in i.get("instance_type","").lower()]
            print(f"  GPU instances: {[i['instance_type'] for i in gpu]}")
    else:
        print(f"  Response: {r.text[:300]}")

        # Probeer ook DataCrunch endpoint
        r2 = requests.post(
            "https://api.datacrunch.io/v1/oauth2/token",
            json={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=15
        )
        print(f"Token endpoint (datacrunch.io) → status {r2.status_code}")
        if r2.status_code == 200:
            token = r2.json()["access_token"]
            print("  Token opgehaald via datacrunch.io ✓")
            r3 = requests.get(
                "https://api.datacrunch.io/v1/instance-types",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15
            )
            print(f"  /instance-types → status {r3.status_code}: {r3.text[:300]}")
        else:
            print(f"  Response: {r2.text[:200]}")


# ══════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════

def main():
    print("ECS — API discovery run")
    print("=" * 50)
    debug_gcore()
    debug_ovh()
    debug_verda()

    # Schrijf lege prices.json zodat de commit-stap niet faalt
    with open("prices.json", "w") as f:
        json.dump({"date": date.today().isoformat(), "status": "discovery run"}, f)


if __name__ == "__main__":
    main()
