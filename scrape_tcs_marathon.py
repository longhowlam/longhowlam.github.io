import csv
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

EVENT_ID = "e5066f0f-f66c-405f-9983-15fbf286ec7d"
BASE_URL = "https://eventresults-api.speedhive.com/sporthive"


def fetch_json(url: str, retries: int = 4, timeout: int = 20):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {exc}") from exc
            time.sleep(0.5 * (2 ** attempt))
    return {}


def fetch_page(race_id: str, page: int) -> dict:
    url = f"{BASE_URL}/races/{race_id}/participants?page={page}&size=10&category=ALL_RESULTS"
    return fetch_json(url)


def normalize_split_prefix(split_name: str) -> str:
    clean = split_name.strip().lower().replace(",", ".").replace(" ", "")
    clean = re.sub(r"[^a-z0-9.]", "_", clean).replace(".", "_")
    return f"split_{clean}"


def parse_age_category(race_category: str | None) -> tuple[str, int | str, int | str]:
    """Derive (age_bracket, age_min, age_max) from Sporthive raceCategory code."""
    if not race_category:
        return "", "", ""
    cat = race_category.strip()
    low = cat.lower()

    if low in ("msen", "vsen", "xsen", "sen"):
        return "18-34", 18, 34

    m = re.match(r"^[mvxMVX]?(\d{1,2})\+?$", cat)
    if m:
        age = int(m.group(1))
        if age < 18:
            # Kids Run exact age (e.g. M4..M12, V4..V12, X6)
            return str(age), age, age
        if age >= 80:
            return f"{age}+", age, ""
        return f"{age}-{age + 4}", age, age + 4

    return "", "", ""


def scrape_race(race_summary: dict, races_dir: Path | None = None, force: bool = False) -> tuple[dict, list[dict]]:
    race_id = race_summary["id"]
    race_name = race_summary.get("raceName", race_id)
    race_meta = fetch_json(f"{BASE_URL}/races/{race_id}")

    first_page = fetch_page(race_id, 0)
    total_pages = first_page.get("totalPages", 0)
    total_elements = first_page.get("totalElements", 0)
    print(f"\n[{race_name}] totalElements: {total_elements}, totalPages: {total_pages}")

    if total_elements == 0 or total_pages == 0:
        return race_meta, []

    if races_dir and not force:
        slug = slugify(race_meta.get("raceName", race_name))
        cache_path = races_dir / f"{slug}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_parts = cached.get("participants") or []
                if len(cached_parts) == total_elements:
                    print(f"  [{race_name}] Using cached {len(cached_parts)} participants (unchanged count).")
                    return race_meta, cached_parts
            except Exception:
                pass

    by_key = {}
    for item in first_page.get("content", []):
        key = item.get("id") or item.get("bib")
        by_key[key] = item

    pages_to_fetch = list(range(1, total_pages))
    max_seen_pages = total_pages

    if pages_to_fetch:
        with ThreadPoolExecutor(max_workers=16) as executor:
            future_to_page = {executor.submit(fetch_page, race_id, p): p for p in pages_to_fetch}
            completed = 0
            for future in as_completed(future_to_page):
                data = future.result()
                completed += 1
                if completed % 100 == 0 or completed == len(pages_to_fetch):
                    print(f"  [{race_name}] {completed}/{len(pages_to_fetch)} pages fetched...")

                page_total = data.get("totalPages", 0)
                if page_total > max_seen_pages:
                    max_seen_pages = page_total

                for item in data.get("content", []):
                    key = item.get("id") or item.get("bib")
                    by_key[key] = item

    if max_seen_pages > total_pages:
        extra_pages = list(range(total_pages, max_seen_pages))
        print(f"  [{race_name}] Fetching {len(extra_pages)} newly added page(s)...")
        with ThreadPoolExecutor(max_workers=16) as executor:
            for data in executor.map(lambda p: fetch_page(race_id, p), extra_pages):
                for item in data.get("content", []):
                    key = item.get("id") or item.get("bib")
                    by_key[key] = item

    participants = sorted(
        by_key.values(),
        key=lambda x: (x.get("overallPosition") or 999999, str(x.get("bib") or "")),
    )
    return race_meta, participants


def flatten_participant(p: dict, race_meta: dict) -> dict:
    custom_values = p.get("customValues") or []
    netto_tot_rank = custom_values[0] if len(custom_values) > 0 else ""
    netto_cat_rank = custom_values[1] if len(custom_values) > 1 else ""

    legs = p.get("legs") or []
    main_leg = legs[0] if legs else {}
    splits = main_leg.get("participantSplits") or []

    race_category = p.get("raceCategory")
    age_bracket, age_min, age_max = parse_age_category(race_category)

    row = {
        "race_id": race_meta.get("id"),
        "race_name": race_meta.get("raceName"),
        "race_date": race_meta.get("date"),
        "race_distance_meters": race_meta.get("distanceInMeter"),
        "overall_position": p.get("overallPosition"),
        "bib": p.get("bib"),
        "name": p.get("name"),
        "gender": p.get("gender"),
        "gender_position": p.get("genderPosition"),
        "gender_participants_count": p.get("genderPositionCount"),
        "race_category": race_category,
        "age_bracket": age_bracket,
        "age_min": age_min,
        "age_max": age_max,
        "category_position": p.get("categoryPosition"),
        "category_participants_count": p.get("numberOfParticipantsInCategory"),
        "country": p.get("country"),
        "city": p.get("city"),
        "team_name": p.get("teamName"),
        "team_id": p.get("teamId"),
        "team_rank": p.get("teamRank"),
        "team_athlete_score": p.get("teamAthleteScore"),
        "team_score": p.get("teamScore"),
        "team_contribute": p.get("teamContribute"),
        "gun_time": p.get("gunTimeOfParticipant"),
        "chip_time": p.get("chipTimeOfParticipant"),
        "netto_total_rank": netto_tot_rank,
        "netto_category_rank": netto_cat_rank,
        "average_speed_kmh": main_leg.get("kmh"),
        "dns": p.get("dns"),
        "dnf": p.get("dnf"),
        "dsq": p.get("dsq"),
        "participant_id": p.get("id"),
        "vid": p.get("vid"),
    }

    for s in splits:
        split_name = s.get("splitName")
        if not split_name:
            continue
        prefix = normalize_split_prefix(split_name)
        row[f"{prefix}_total_time"] = s.get("totalSplitDuration", "")
        row[f"{prefix}_split_time"] = s.get("splitDuration", "")
        row[f"{prefix}_speed_kmh"] = s.get("personalSplitSpeed", "")
        row[f"{prefix}_distance_m"] = s.get("distanceInMeters", "")

    return row


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    base_cols = []
    split_cols = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                if k.startswith("split_"):
                    split_cols.append(k)
                else:
                    base_cols.append(k)
    fieldnames = base_cols + split_cols
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    out_dir = Path(__file__).resolve().parent
    races_dir = out_dir / "races"
    races_dir.mkdir(exist_ok=True)

    print(f"Fetching all races for event {EVENT_ID}...")
    races = fetch_json(f"{BASE_URL}/events/{EVENT_ID}/races")
    print(f"Found {len(races)} races in event.")

    all_raw_races = []
    all_flattened_rows = []

    for r_summary in races:
        race_meta, participants = scrape_race(r_summary, races_dir=races_dir)
        race_name = race_meta.get("raceName", r_summary.get("raceName", "race"))
        slug = slugify(race_name)

        all_raw_races.append(
            {
                "race_metadata": race_meta,
                "total_participants_scraped": len(participants),
                "participants": participants,
            }
        )

        if participants:
            race_rows = [flatten_participant(p, race_meta) for p in participants]
            all_flattened_rows.extend(race_rows)

            race_csv = races_dir / f"{slug}.csv"
            race_json = races_dir / f"{slug}.json"
            write_csv(race_csv, race_rows)
            with open(race_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "event_id": EVENT_ID,
                        "race_metadata": race_meta,
                        "scraped_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "total_participants_scraped": len(participants),
                        "participants": participants,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            print(f"  -> Saved {len(participants)} participants to {race_csv.name} & {race_json.name}")

    all_json_path = out_dir / "all_races_participants.json"
    all_csv_path = out_dir / "all_races_participants.csv"

    with open(all_json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "event_id": EVENT_ID,
                "scraped_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_participants_scraped": len(all_flattened_rows),
                "races": all_raw_races,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    write_csv(all_csv_path, all_flattened_rows)
    print(f"\nAll done! Total participants across all races: {len(all_flattened_rows)}")
    print(f"  - Combined CSV:  {all_csv_path}")
    print(f"  - Combined JSON: {all_json_path}")
    print(f"  - Per-race files: {races_dir}")


if __name__ == "__main__":
    main()
