"""Standalone WSGI entry point for the queue-backed API.

Production example: gunicorn --bind 0.0.0.0:$PORT 'web.standalone:create_app()'
"""
from __future__ import annotations

import os
from flask import Flask, jsonify

from web.intake_api import create_intake_blueprint
from web.analytics_api import create_analytics_blueprint
from web.calendar_api import create_calendar_blueprint
from web.review_api import create_review_blueprint
from web.job_queue import init_db


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1_100_000
    db_path = os.getenv("LEAD_QUALIFIER_DB", "instance/lead_qualifier.sqlite3")
    init_db(db_path)
    app.register_blueprint(create_intake_blueprint(db_path=db_path))
    app.register_blueprint(create_review_blueprint(db_path=db_path))
    app.register_blueprint(create_analytics_blueprint(db_path=db_path))
    app.register_blueprint(create_calendar_blueprint(db_path=db_path))

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok", service="lead-qualifier-agent"), 200

    @app.get("/readyz")
    def readyz():
        try:
            init_db(db_path)
        except Exception:
            app.logger.exception("Readiness database check failed")
            return jsonify(status="not_ready"), 503
        return jsonify(status="ready"), 200

    return app
