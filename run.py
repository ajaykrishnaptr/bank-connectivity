"""Run the app locally: `python run.py`, then open https://localhost:5000.

`debug=True` enables the auto-reloader and the in-browser debugger. NEVER use
it in production: the debugger lets anyone with HTTP access execute Python on
the server. `ssl_context='adhoc'` serves HTTPS with a self-signed certificate,
which UniCredit's redirect URI (https://localhost:5000/callback) requires.
"""
from fintnet.app import app

if __name__ == "__main__":
    app.run(debug=True, ssl_context="adhoc")
