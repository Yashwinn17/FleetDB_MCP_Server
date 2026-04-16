"""Seed the fleet database with realistic demo data.

Run:
    python -m fleetdb_mcp.seed
"""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

from .config import load_settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

random.seed(42)

DRIVERS = [
    ("Miguel", "Alvarez", "TX-DL-100234"),
    ("Priya", "Raman", "TX-DL-100235"),
    ("Jordan", "Whitaker", "TX-DL-100236"),
    ("Amara", "Okonkwo", "TX-DL-100237"),
    ("Sven", "Lindqvist", "TX-DL-100238"),
    ("Keiko", "Matsuda", "TX-DL-100239"),
    ("Luis", "Cardoso", "TX-DL-100240"),
    ("Harper", "Chen", "TX-DL-100241"),
]

MAKES_MODELS = [
    ("Ford", "F-150", "diesel"),
    ("Ford", "Transit", "diesel"),
    ("Nissan", "NV200", "gasoline"),
    ("Nissan", "Leaf", "electric"),
    ("Tesla", "Model 3", "electric"),
    ("Toyota", "Prius", "hybrid"),
    ("Chevrolet", "Silverado", "gasoline"),
    ("Ram", "ProMaster", "diesel"),
]

CITIES = ["Dallas", "Fort Worth", "Plano", "Arlington", "Irving", "Frisco", "Garland", "McKinney"]


def _redact_dsn(dsn: str) -> str:
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn

    netloc = parts.hostname or ""
    if parts.username:
        netloc = f"{parts.username}:***@{netloc}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _reset(conn: psycopg.AsyncConnection) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "TRUNCATE trips, maintenance_events, vehicles, drivers, mcp_audit_log "
            "RESTART IDENTITY CASCADE"
        )


async def _seed_drivers(conn: psycopg.AsyncConnection) -> list[int]:
    ids: list[int] = []
    async with conn.cursor() as cur:
        for first, last, lic in DRIVERS:
            hired = date.today() - timedelta(days=random.randint(200, 2000))
            expiry = date.today() + timedelta(days=random.randint(-15, 1200))
            await cur.execute(
                """
                INSERT INTO drivers (first_name, last_name, license_number,
                                     license_expiry, hired_at, active)
                VALUES (%s,%s,%s,%s,%s, TRUE)
                RETURNING driver_id
                """,
                (first, last, lic, expiry, hired),
            )
            row = await cur.fetchone()
            ids.append(row["driver_id"])
    return ids


async def _seed_vehicles(conn: psycopg.AsyncConnection) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    async with conn.cursor() as cur:
        for i in range(20):
            make, model, fuel = random.choice(MAKES_MODELS)
            year = random.randint(2018, 2024)
            vin = f"1HG{make[:2].upper()}{random.randint(100000, 999999):06d}{i:03d}"[:17].ljust(17, "X")
            odo = random.randint(5_000, 180_000)
            status = random.choices(
                ["active", "active", "active", "maintenance", "retired"],
                weights=[60, 20, 10, 7, 3],
            )[0]
            acquired = date.today() - timedelta(days=random.randint(100, 1800))
            await cur.execute(
                """
                INSERT INTO vehicles (vin, make, model, year, fuel_type,
                                      odometer_km, status, acquired_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING vehicle_id
                """,
                (vin, make, model, year, fuel, odo, status, acquired),
            )
            row = await cur.fetchone()
            out.append((row["vehicle_id"], fuel))
    return out


async def _seed_maintenance(conn: psycopg.AsyncConnection, vehicles: list[tuple[int, str]]) -> None:
    events = ["oil_change", "tire_rotation", "brake_service", "battery_replacement", "inspection", "repair"]
    async with conn.cursor() as cur:
        for vid, _ in vehicles:
            for _ in range(random.randint(1, 8)):
                etype = random.choice(events)
                cost = round(random.uniform(45, 1500), 2)
                dt = date.today() - timedelta(days=random.randint(1, 365))
                hours = round(random.uniform(0.5, 24.0), 1)
                await cur.execute(
                    """
                    INSERT INTO maintenance_events (vehicle_id, event_date,
                        event_type, cost_usd, downtime_hours, notes)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (vid, dt, etype, cost, hours, f"{etype.replace('_', ' ')} by shop"),
                )


async def _seed_trips(
    conn: psycopg.AsyncConnection,
    vehicles: list[tuple[int, str]],
    drivers: list[int],
) -> None:
    async with conn.cursor() as cur:
        for _ in range(200):
            vid, fuel = random.choice(vehicles)
            did = random.choice(drivers)
            start = datetime.now() - timedelta(
                days=random.randint(0, 90),
                hours=random.randint(0, 23),
            )
            duration = timedelta(hours=random.uniform(0.5, 8))
            end = start + duration
            distance = round(random.uniform(5, 300), 2)
            # Electric vehicles report 0 fuel used; others scale roughly by distance.
            fuel_used = 0 if fuel == "electric" else round(distance / random.uniform(8, 15), 2)
            origin, dest = random.sample(CITIES, 2)
            await cur.execute(
                """
                INSERT INTO trips (vehicle_id, driver_id, started_at, ended_at,
                                   origin, destination, distance_km, fuel_used_l)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (vid, did, start, end, origin, dest, distance, fuel_used),
            )


async def main() -> None:
    settings = load_settings()
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url,
            row_factory=dict_row,
            autocommit=False,
        ) as conn:
            print("* clearing existing data")
            await _reset(conn)
            print("* seeding drivers")
            drivers = await _seed_drivers(conn)
            print("* seeding vehicles")
            vehicles = await _seed_vehicles(conn)
            print("* seeding maintenance events")
            await _seed_maintenance(conn, vehicles)
            print("* seeding trips")
            await _seed_trips(conn, vehicles, drivers)
            await conn.commit()
            print(f"seeded {len(drivers)} drivers, {len(vehicles)} vehicles")
    except psycopg.OperationalError as exc:
        redacted_dsn = _redact_dsn(settings.database_url)
        raise SystemExit(
            "Could not connect to Postgres for seeding.\n"
            f"DATABASE_URL: {redacted_dsn}\n\n"
            "The default local setup expects:\n"
            "  postgresql://fleet:fleet@localhost:5432/fleetdb\n\n"
            "If you started Postgres with docker-compose and still get password authentication failed,\n"
            "the most common cause is an existing Docker volume initialized with older credentials.\n"
            "Either:\n"
            "  1. Set DATABASE_URL to the real username/password for your running database, or\n"
            "  2. Recreate the local database volume so docker-compose can initialize it with fleet/fleet.\n\n"
            f"Original error: {exc}"
        ) from exc


if __name__ == "__main__":
    asyncio.run(main())
