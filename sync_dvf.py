from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import requests

DEPARTMENT = "83"
ALLOWED_INSEE_CODES = {"83118", "83061", "83107"}
YEARS = list(range(2021, datetime.now(timezone.utc).year))
BASE_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv/{year}/departements/{dep}.csv.gz"
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / f"dvf_{DEPARTMENT}.duckdb"
META_PATH = DATA_DIR / "dvf_metadata.json"
KEEP = [
    "id_mutation", "date_mutation", "nature_mutation", "valeur_fonciere",
    "adresse_numero", "adresse_suffixe", "adresse_nom_voie", "code_postal",
    "code_commune", "nom_commune", "type_local", "surface_reelle_bati",
    "nombre_pieces_principales", "surface_terrain", "id_parcelle",
    "latitude", "longitude",
]


def fetch_year(year: int) -> pd.DataFrame:
    url = BASE_URL.format(year=year, dep=DEPARTMENT)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    tmp = DATA_DIR / f"dvf_{DEPARTMENT}_{year}.csv.gz"
    tmp.write_bytes(response.content)
    try:
        frame = pd.read_csv(tmp, compression="gzip", low_memory=False)
    finally:
        tmp.unlink(missing_ok=True)

    frame = frame[[column for column in KEEP if column in frame.columns]].copy()
    frame = frame[frame["nature_mutation"].astype(str).str.lower().eq("vente")]
    frame = frame[frame["type_local"].isin(["Maison", "Appartement"])]
    for column in [
        "valeur_fonciere", "surface_reelle_bati", "nombre_pieces_principales",
        "surface_terrain", "latitude", "longitude",
    ]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["date_mutation"] = pd.to_datetime(frame["date_mutation"], errors="coerce")
    frame = frame[
        frame["valeur_fonciere"].gt(0)
        & frame["surface_reelle_bati"].gt(0)
    ]
    frame = frame.sort_values(
        ["id_mutation", "surface_reelle_bati"],
        ascending=[True, False],
    )
    frame = frame.drop_duplicates(["id_mutation", "type_local"], keep="first")
    frame["adresse"] = (
        frame["adresse_numero"].fillna("").astype(str).str.replace(".0", "", regex=False)
        + " " + frame["adresse_suffixe"].fillna("").astype(str)
        + " " + frame["adresse_nom_voie"].fillna("").astype(str)
    ).str.replace(r"\s+", " ", regex=True).str.strip()
    frame["prix_m2"] = frame["valeur_fonciere"] / frame["surface_reelle_bati"]
    frame["millesime"] = year
    return frame


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    frames: list[pd.DataFrame] = []
    loaded_years: list[int] = []
    errors: dict[str, str] = {}

    for year in YEARS:
        try:
            frame = fetch_year(year)
            frames.append(frame)
            loaded_years.append(year)
            print(f"OK {year}: {len(frame):,} lignes")
        except Exception as exc:
            errors[str(year)] = str(exc)
            print(f"ERREUR {year}: {exc}")

    if not frames:
        raise SystemExit("Aucun millésime DVF n'a pu être téléchargé.")

    full = pd.concat(frames, ignore_index=True, sort=False)
    commune_codes = (
        full["code_commune"].astype(str)
        .str.replace(".0", "", regex=False)
        .str.strip()
    )
    full = full[commune_codes.isin(ALLOWED_INSEE_CODES)].copy()
    full = full.drop_duplicates(["id_mutation", "type_local"], keep="last")
    full = full[full["prix_m2"].between(300, 100000)]

    connection = duckdb.connect(str(DB_PATH))
    try:
        connection.execute("DROP TABLE IF EXISTS mutations")
        connection.register("dvf_frame", full)
        connection.execute("CREATE TABLE mutations AS SELECT * FROM dvf_frame")
        connection.execute("CREATE INDEX idx_commune ON mutations(code_commune)")
        connection.execute("CREATE INDEX idx_date ON mutations(date_mutation)")
        connection.execute("CREATE INDEX idx_type ON mutations(type_local)")
    finally:
        connection.close()

    metadata = {
        "department": DEPARTMENT,
        "source": "DVF géolocalisées data.gouv.fr / DGFiP",
        "source_url": "https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "years_loaded": loaded_years,
        "rows": int(len(full)),
        "latest_transaction": (
            full["date_mutation"].max().date().isoformat() if len(full) else None
        ),
        "errors": errors,
    }
    META_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
