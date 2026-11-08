Build a small Flask web application in this directory (it is empty). Python 3.10, stdlib + Flask only, SQLite via sqlite3.

Requirements:
- `app.py` is the entry point and creates the app; split the rest into modules: `auth.py` (register, login, logout, password hashing with hashlib+salt, session cookie), `db.py` (connection, schema init, query helpers), `models.py` (User, Note dataclasses and their load/save functions), `notes.py` (CRUD blueprint for notes owned by the logged-in user), `api.py` (JSON API blueprint: /api/notes GET/POST, /api/notes/<id> DELETE, token auth via header), `errors.py` (error handlers 404/403/500 that render JSON or HTML depending on Accept), `templates/` with simple Jinja pages.
- Login must be rate-limited: after 5 failed attempts per username in 10 minutes, respond 429.
- Notes can be shared read-only via a /share/<token> link.
- Write `tests/test_app.py` with pytest covering register/login/notes CRUD/sharing/api token and rate-limit.
- Create a venv-free setup: `requirements.txt` with flask and pytest.
Work incrementally: create the files one at a time, then run the tests with `python3 -m pytest -q` (install with `pip3 install --user flask pytest` if missing) and fix whatever fails by editing the existing files. Do not create a git repo. Finish when the tests pass.
