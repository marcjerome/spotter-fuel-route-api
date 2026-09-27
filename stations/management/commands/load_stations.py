import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from stations.geocoding import US_STATES, get_place_index
from stations.models import FuelStation


class Command(BaseCommand):
    help = "Import the fuel price CSV into FuelStation, geocoding each station by city."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            dest="csv_path",
            type=Path,
            default=settings.DATA_DIR / "fuel-prices.csv",
            help="Path to the fuel price CSV (default: data/fuel-prices.csv).",
        )
        parser.add_argument(
            "--if-empty", action="store_true", help="Skip the import when stations are already loaded."
        )

    def handle(self, *args, csv_path: Path, if_empty: bool = False, **options):
        if if_empty and FuelStation.objects.exists():
            self.stdout.write("Stations already loaded; skipping import.")
            return
        rows = self._read_rows(csv_path)
        index = get_place_index()

        # The sheet lists some OPIS ids more than once (brand name variants);
        # keep the cheapest price seen for each id.
        stations: dict[int, FuelStation] = {}
        skipped_non_us = 0
        for row in rows:
            state = row["State"].strip().upper()
            if state not in US_STATES:
                skipped_non_us += 1
                continue
            opis_id = int(row["OPIS Truckstop ID"])
            price = Decimal(row["Retail Price"].strip())
            existing = stations.get(opis_id)
            if existing and existing.retail_price <= price:
                continue
            city = row["City"].strip()
            place = index.lookup(city, state)
            stations[opis_id] = FuelStation(
                opis_id=opis_id,
                name=row["Truckstop Name"].strip(),
                address=row["Address"].strip(),
                city=city,
                state=state,
                rack_id=int(row["Rack ID"]) if row["Rack ID"].strip() else None,
                retail_price=price,
                latitude=place.lat if place else None,
                longitude=place.lng if place else None,
            )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values(), batch_size=1000)

        geocoded = sum(1 for s in stations.values() if s.has_coordinates)
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(stations)} US stations from {len(rows)} rows "
                f"({geocoded} geocoded, {len(stations) - geocoded} without coordinates, "
                f"{skipped_non_us} non-US rows skipped)."
            )
        )

    @staticmethod
    def _read_rows(path: Path) -> list[dict[str, str]]:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
