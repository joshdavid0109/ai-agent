python3 -m pip install fastapi uvicorn huggingface_hub jinja2

source /Users/joshuadanieldavid/Downloads/github_repo/ai-agent/.venv/bin/activate

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000