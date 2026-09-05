# DEV HOSTING

Owner: @founderdvx

## Admin login
- URL: `/login`
- Username: `devxd`
- Password: `devxd`

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn --bind 0.0.0.0:$PORT app:app`

The panel runs uploaded Python files directly with the server's Python interpreter. `main.py`, `app.py`, `bot.py`, `run.py`, or `index.py` are preferred; otherwise the first `.py` file is used.

ZIP uploads are extracted safely. Path traversal in ZIPs and file operations is rejected.
