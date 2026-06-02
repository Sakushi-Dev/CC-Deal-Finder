"""Entry point: starts the modular CollectorCrypt app.

The actual logic lives in the ``collectorcrypt`` package.
"""
from collectorcrypt import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
