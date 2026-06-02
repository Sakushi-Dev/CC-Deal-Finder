"""Entry-Point: startet die modulare CollectorCrypt-App.

Die eigentliche Logik liegt im Paket ``collectorcrypt``.
"""
from collectorcrypt import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
