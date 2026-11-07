"""Explicit, read-only import of the legacy PostgreSQL database."""

from uuid import NAMESPACE_URL, uuid5

from ..config import ConfigError, load_config
from .db import Storage


def import_postgres(cfg_path, destination: Storage):
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        raise ConfigError("Install notegram[migration] to import PostgreSQL data") from None
    raw = load_config(cfg_path)
    args = dict(raw.get("db", {}))
    if not args:
        raise ConfigError("The selected config has no [notegram.db] table")
    if "database" in args:
        args["dbname"] = args.pop("database")
    args["connect_timeout"] = 10
    counts = {"events": 0, "subscriptions": 0}
    # A repeatable snapshot, explicitly read-only: no schema changes to the old DB.
    with psycopg.connect(**args) as source:
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        columns = {}
        for table in ("message", "tg"):
            names = {
                row[0]
                for row in source.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                    (table,),
                )
            }
            columns[table] = "domain" if "domain" in names else "domain_id"
        identity = str(
            uuid5(NAMESPACE_URL, "|".join(str(args.get(k, "")) for k in ("host", "port", "dbname")))
        )
        with destination.connect() as target:
            # One target transaction. Imported history is deliberately not sent to Telegram.
            with source.cursor(name="notegram_import") as cursor:
                cursor.execute(
                    sql.SQL("""SELECT m.id,d.name,m.level,m.ts,m.content,m.markdown
                    FROM public.message m JOIN public.domain d ON d.id=m.{} ORDER BY m.id""").format(
                        sql.Identifier(columns["message"])
                    )
                )
                for mid, domain, level, ts, message, markdown in cursor:
                    event = {
                        "id": f"legacy-{identity}-{mid}",
                        "domain": domain,
                        "level": level,
                        "timestamp": ts,
                        "message": message,
                        "format": "markdown" if markdown else "plain",
                    }
                    counts["events"] += destination.post(event, schedule=False, connection=target)
            rows = source.execute(
                sql.SQL("""SELECT d.name,t.cid,t.report_level FROM public.tg t
                JOIN public.domain d ON d.id=t.{}""").format(sql.Identifier(columns["tg"]))
            )
            for domain, cid, level in rows:
                destination.link_chat(
                    domain, cid, 30 if level is None else level, connection=target
                )
                counts["subscriptions"] += 1
    return counts
