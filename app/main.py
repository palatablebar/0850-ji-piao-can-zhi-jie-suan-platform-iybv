import os

from flask import Flask, jsonify, render_template
from werkzeug.exceptions import RequestEntityTooLarge

from app.agent_delivery.http import agent_api
from app.catalog import SPEC
from app.config import MAX_FILE, ROOT
from app.errors import DomainIssue, error_response
from app.review.store import Store
from app.routes import api


def create_app(database=None):
    app = Flask(__name__)
    db_path = database or os.getenv("WORKBENCH_DB", str(ROOT / "instance" / "workbench.sqlite3"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 64 * 1024
    app.config["WORKBENCH_DATABASE"] = db_path
    run_store = Store(db_path)
    app.extensions["runs"] = run_store
    app.extensions["agent_deliveries"] = run_store
    app.register_error_handler(DomainIssue, error_response)
    app.register_error_handler(RequestEntityTooLarge, lambda _: error_response(DomainIssue("FILE_TOO_LARGE", 413)))
    app.register_blueprint(api)
    app.register_blueprint(agent_api)

    @app.get("/")
    def home():
        return render_template("index.html", p=SPEC)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()
