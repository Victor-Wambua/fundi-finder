"""Flask CLI commands: ``flask seed`` and ``flask scan-abuse``."""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from flask import Flask

from .db import Base, get_db


def register(app: Flask) -> None:
    @app.cli.command("seed")
    @click.option("--reset", is_flag=True, help="Drop all data and uploads first.")
    @click.option("--random-seed", default=42, show_default=True, type=int)
    def seed_command(reset: bool, random_seed: int) -> None:
        """Fill the database with synthetic demo data."""
        from .seed import DEMO_PASSWORD, SeedError, seed_demo

        if reset:
            get_db().close()
            Base.metadata.drop_all(app.extensions["engine"])
            Base.metadata.create_all(app.extensions["engine"])
            uploads = Path(app.config["INSTANCE_DIR"]) / "uploads"
            shutil.rmtree(uploads, ignore_errors=True)
            from .uploads import Storage

            app.extensions["storage"] = Storage(uploads)
        try:
            summary = seed_demo(app, random_seed)
        except SeedError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Seeded {summary.fundis} fundis, {summary.customers} customers, {summary.jobs} jobs "
            f"({summary.completed_jobs} completed), {summary.reviews} reviews, "
            f"{summary.id_documents} placeholder ID documents, {summary.reports} reports, "
            f"{summary.abuse_flags} abuse flags."
        )
        click.echo(
            "Demo logins (all synthetic): customer@fundifinder.test, fundi@fundifinder.test,"
        )
        click.echo(f"admin@fundifinder.test, admin2@fundifinder.test. Password: {DEMO_PASSWORD}")

    @app.cli.command("scan-abuse")
    def scan_command() -> None:
        """Run the review-ring and duplicate-account heuristics and record new flags."""
        from . import abuse_scan

        db = get_db()
        created = abuse_scan.scan(db)
        db.commit()
        click.echo(f"{len(created)} new flag(s).")
