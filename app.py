import logging
import os

import click
from flask import Flask, current_app
from models import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app() -> Flask:
    app = Flask(__name__)
    # DSN передаётся окружением; дефолт — локальный Postgres, чтобы CLI
    # поднимался и без переменной (реальный DSN задаётся на проде).
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "SQLALCHEMY_DATABASE_URI", "postgresql+psycopg2://localhost/amo")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    register_cli(app)
    return app


def register_cli(app: Flask) -> None:
    @app.cli.command("create-tables",
                     help="Создать таблицы (для разработки; в бою — миграции).")
    def create_tables():
        db.create_all()
        click.echo("Таблицы созданы.")

    @app.cli.command("sync", help="Ночная синхронизация с amoCRM.")
    @click.option("--incremental", is_flag=True, default=False,
                  help="Инкрементальный прогон без чистки.")
    def sync(incremental: bool):
        if not current_app.config.get("SQLALCHEMY_DATABASE_URI"):
            raise click.UsageError("Задайте SQLALCHEMY_DATABASE_URI")
        from main import collect_leads_customers  # импорт после инициализации db
        collect_leads_customers(is_full=not incremental)


app = create_app()
