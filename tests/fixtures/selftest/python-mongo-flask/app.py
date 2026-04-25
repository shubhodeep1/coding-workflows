from flask import Flask, Response, jsonify


app = Flask(__name__)


@app.get("/health")
def health() -> tuple[Response, int]:
	return jsonify({"status": "ok"}), 200
