from flask import Flask, jsonify


app = Flask(__name__)


@app.get("/health")
def health() -> tuple[dict[str, str], int]:
	return jsonify({"status": "ok"}), 200
