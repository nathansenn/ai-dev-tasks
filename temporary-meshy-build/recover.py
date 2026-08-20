#!/usr/bin/env python3
"""Recover already-completed Meshy refine tasks without spending more credits."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from generate import ASSETS, BuildError, download, request_json, sha256

OUT = Path("build-output")
REFINE_TASKS = {
    "windmill-workshop": "01a01c5f-9767-7d3c-a7cb-372ff7db79bb",
    "millstone-grain-chute": "01a01c5f-9bff-7d3d-9d75-0bca3e138851",
}
PREVIEW_TASKS = {
    "windmill-workshop": "01a01c5d-b09b-7e7a-bcd9-856d6ee29da3",
    "millstone-grain-chute": "01a01c5d-b65a-7e7a-b169-7e0f4f2de7f5",
}


def main() -> None:
    if not os.environ.get("MESHY_API_KEY", "").startswith("msy_"):
        raise BuildError("MESHY_API_KEY is missing or malformed")
    OUT.mkdir(parents=True, exist_ok=True)
    balance = int(request_json("GET", "/openapi/v1/balance").get("balance", 0))
    records = []
    for asset in ASSETS:
        task_id = REFINE_TASKS[asset.slug]
        result = request_json("GET", f"/openapi/v2/text-to-3d/{task_id}")
        if result.get("status") != "SUCCEEDED":
            raise BuildError(f"Refine task is not recoverable: {asset.slug} {result.get('status')}")
        model_url = (result.get("model_urls") or {}).get("glb")
        if not model_url:
            raise BuildError(f"No GLB URL on recovered task: {asset.slug}")
        model_path = OUT / asset.filename
        download(str(model_url), model_path)
        thumb_url = result.get("alpha_thumbnail_url") or result.get("thumbnail_url")
        thumb_path = OUT / f"{asset.slug}-preview.png"
        if thumb_url:
            download(str(thumb_url), thumb_path)
        records.append({
            **asdict(asset),
            "preview_task_id": PREVIEW_TASKS[asset.slug],
            "refine_task_id": task_id,
            "refine_status": result.get("status"),
            "raw_bytes": model_path.stat().st_size,
            "raw_sha256": sha256(model_path),
            "thumbnail": thumb_path.name if thumb_path.exists() else None,
        })
    manifest = {
        "schema": 1,
        "generator": "Meshy Text-to-3D API v2",
        "ai_model": "latest (Meshy 7)",
        "generated_at": "2026-08-19T23:31:37Z",
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "lesson": "gear-ratio",
        "credits_consumed_original_build": 70,
        "balance_at_recovery": balance,
        "assets": records,
        "security": "The API credential was supplied through a one-time RSA-OAEP-SHA256 handoff and was never committed.",
    }
    (OUT / "meshy-generation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Recovered two completed Meshy assets without creating new tasks or consuming credits.")


if __name__ == "__main__":
    try:
        main()
    except BuildError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
