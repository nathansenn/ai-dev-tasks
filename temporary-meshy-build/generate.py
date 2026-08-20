#!/usr/bin/env python3
"""Generate two web-oriented AAA assets for Commander Academy using Meshy v2."""
from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API = "https://api.meshy.ai"
OUT = Path("build-output")
POLL_SECONDS = 10
TIMEOUT_SECONDS = 45 * 60


@dataclass(frozen=True)
class Asset:
    slug: str
    filename: str
    target_polycount: int
    prompt: str
    texture_prompt: str


ASSETS = (
    Asset(
        slug="windmill-workshop",
        filename="gear-ratio-workshop-meshy.raw.glb",
        target_polycount=60000,
        prompt=(
            "One connected open-front medieval windmill gristmill workshop environment for an AAA educational game. "
            "Weathered timber post-and-beam frame on a rough stone plinth, steep wood-shingle roof, four wooden lattice sails high on the rear gable, side platforms, grain sacks and flour bins at the perimeter. "
            "Keep a large empty unobstructed machinery bay at front center for animated gears. Realistic proportions and a clean silhouette. "
            "No people, text, logos, visible gears, millstone, floating pieces, or separate ground diorama."
        ),
        texture_prompt=(
            "Historically grounded warm weathered oak, hand-laid gray limestone, dark forged iron brackets, muted canvas sacks, dusty flour residue and restrained edge wear. "
            "Realistic physically based materials with clean albedo for custom cinematic lighting. No painted text, baked dramatic shadows, fantasy glow, or bright saturated colors."
        ),
    ),
    Asset(
        slug="millstone-grain-chute",
        filename="gear-ratio-millstone-meshy.raw.glb",
        target_polycount=40000,
        prompt=(
            "One connected medieval gristmill millstone and grain chute machine for an AAA educational game. "
            "Two stacked circular granite stones in a stout oak housing, central iron spindle, timber grain hopper above, shaped wooden feed chute, flour collection trough and tied canvas sack below. "
            "Every part physically connected with believable mechanical construction, realistic proportions and a clean silhouette, isolated with origin at its base. "
            "No people, text, logos, loose gears, floor diorama, or floating pieces."
        ),
        texture_prompt=(
            "Rough speckled granite millstones with radial tool marks, aged oak frame and hopper, blackened forged iron spindle, pale flour dust in creases, natural canvas sack. "
            "Realistic physically based materials, controlled roughness and subtle wear. No baked hard shadows, labels, fantasy glow, or decorative text."
        ),
    ),
)


class BuildError(RuntimeError):
    pass


def key() -> str:
    value = os.environ.get("MESHY_API_KEY", "").strip()
    if not value.startswith("msy_"):
        raise BuildError("MESHY_API_KEY is missing or malformed")
    return value


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {key()}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(6):
        try:
            response = requests.request(method, API + path, headers=headers, json=payload, timeout=90)
            if response.status_code == 429 or response.status_code >= 500:
                delay = min(60, 5 * (attempt + 1))
                print(f"Transient Meshy HTTP {response.status_code}; retrying in {delay}s", flush=True)
                time.sleep(delay)
                continue
            if not response.ok:
                detail = response.text[:900].replace(key(), "[REDACTED]")
                raise BuildError(f"Meshy HTTP {response.status_code}: {detail}")
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < 5:
                time.sleep(min(45, 4 * (attempt + 1)))
    raise BuildError(f"Meshy request failed after retries: {last}")


def create_preview(asset: Asset) -> str:
    if len(asset.prompt) > 600 or len(asset.texture_prompt) > 600:
        raise BuildError(f"Prompt exceeds Meshy 600-character limit: {asset.slug}")
    result = request_json(
        "POST",
        "/openapi/v2/text-to-3d",
        {
            "mode": "preview",
            "prompt": asset.prompt,
            "model_type": "standard",
            "ai_model": "latest",
            "ultra_mode": True,
            "should_remesh": True,
            "topology": "triangle",
            "target_polycount": asset.target_polycount,
            "target_formats": ["glb"],
            "alpha_thumbnail": True,
            "moderation": True,
        },
    ).get("result")
    if not result:
        raise BuildError(f"Meshy returned no preview id for {asset.slug}")
    print(f"Created {asset.slug} preview task {result}", flush=True)
    return str(result)


def create_refine(asset: Asset, preview_id: str) -> str:
    result = request_json(
        "POST",
        "/openapi/v2/text-to-3d",
        {
            "mode": "refine",
            "preview_task_id": preview_id,
            "ai_model": "latest",
            "enable_pbr": True,
            "texture_resolution": "2k",
            "texture_prompt": asset.texture_prompt,
            "remove_lighting": True,
            "target_formats": ["glb"],
            "alpha_thumbnail": True,
            "moderation": True,
        },
    ).get("result")
    if not result:
        raise BuildError(f"Meshy returned no refine id for {asset.slug}")
    print(f"Created {asset.slug} refine task {result}", flush=True)
    return str(result)


def poll(task_id: str, label: str) -> dict[str, Any]:
    started = time.monotonic()
    previous = None
    while time.monotonic() - started < TIMEOUT_SECONDS:
        data = request_json("GET", f"/openapi/v2/text-to-3d/{task_id}")
        status = str(data.get("status", "UNKNOWN"))
        progress = int(data.get("progress") or 0)
        state = (status, progress)
        if state != previous:
            print(f"{label}: {status} {progress}%", flush=True)
            previous = state
        if status == "SUCCEEDED":
            return data
        if status in {"FAILED", "CANCELED"}:
            error = data.get("task_error") or {}
            raise BuildError(f"{label} {status}: {error.get('message') or data}")
        time.sleep(POLL_SECONDS)
    raise BuildError(f"Timed out waiting for {label} ({task_id})")


def poll_all(tasks: dict[str, str], phase: str) -> dict[str, dict[str, Any]]:
    done: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(poll, task_id, f"{slug} {phase}"): slug for slug, task_id in tasks.items()}
        for future in as_completed(futures):
            slug = futures[future]
            done[slug] = future.result()
    return done


def download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=240) as response:
        response.raise_for_status()
        with path.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    handle.write(chunk)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    before = int(request_json("GET", "/openapi/v1/balance").get("balance", 0))
    print(f"Meshy balance before build: {before}", flush=True)
    if before < 70:
        raise BuildError(f"Insufficient Meshy credits: {before}; this two-asset Ultra build requires 70")

    previews = {asset.slug: create_preview(asset) for asset in ASSETS}
    preview_data = poll_all(previews, "preview")
    refines = {asset.slug: create_refine(asset, previews[asset.slug]) for asset in ASSETS}
    refine_data = poll_all(refines, "refine")

    records: list[dict[str, Any]] = []
    for asset in ASSETS:
        result = refine_data[asset.slug]
        model_url = (result.get("model_urls") or {}).get("glb")
        if not model_url:
            raise BuildError(f"No GLB URL returned for {asset.slug}")
        model_path = OUT / asset.filename
        download(str(model_url), model_path)
        thumbnail_url = result.get("alpha_thumbnail_url") or result.get("thumbnail_url")
        thumbnail_path = OUT / f"{asset.slug}-preview.png"
        if thumbnail_url:
            download(str(thumbnail_url), thumbnail_path)
        records.append(
            {
                **asdict(asset),
                "preview_task_id": previews[asset.slug],
                "refine_task_id": refines[asset.slug],
                "preview_credits": preview_data[asset.slug].get("consumed_credits"),
                "refine_credits": result.get("consumed_credits"),
                "raw_bytes": model_path.stat().st_size,
                "raw_sha256": sha256(model_path),
                "thumbnail": thumbnail_path.name if thumbnail_path.exists() else None,
            }
        )

    after = int(request_json("GET", "/openapi/v1/balance").get("balance", 0))
    manifest = {
        "schema": 1,
        "generator": "Meshy Text-to-3D API v2",
        "ai_model": "latest (Meshy 7)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lesson": "gear-ratio",
        "credits_before": before,
        "credits_after": after,
        "credits_consumed": before - after,
        "assets": records,
        "security": "The API credential was supplied through a one-time RSA-OAEP-SHA256 handoff and was never committed.",
    }
    (OUT / "meshy-generation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Generated two assets; consumed {before - after} Meshy credits", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BuildError as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(1)
