# main.py

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from agents import route
from memory import memory
import time
import uuid

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Allow cross-origin requests so the widget iframe can be embedded
# on any external website
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ====== Widget Route (iframe content) ======
@app.get("/widget", response_class=HTMLResponse)
def widget(request: Request):
    """Serves the chat widget UI — this is the page loaded inside the iframe."""
    return templates.TemplateResponse("index.html", {"request": request})


# ====== Embed Demo Route (host page) ======
@app.get("/", response_class=HTMLResponse)
def embed_demo(request: Request):
    """Demo page showing the widget embedded via iframe on a sample website."""
    return templates.TemplateResponse("embed.html", {"request": request})


# ====== API Routes ======

@app.get("/history/{session_id}")
def get_history(session_id: str):
    history = memory.get_history(session_id)
    return JSONResponse(history)


@app.get("/sessions")
def list_sessions():
    return JSONResponse(memory.get_sessions())


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    memory.delete_session(session_id)
    return {"success": True}


@app.get("/chat")
def chat(prompt: str, session_id: str = None):

    if not session_id:
        session_id = str(uuid.uuid4())

    agent = route(prompt)

    def event_stream():

        try:
            for event in agent.stream_execution(session_id, prompt):

                if event["type"] == "content":
                    # SSE spec: multi-line data must have each line
                    # prefixed with "data: ". Lines are joined by the
                    # browser's EventSource into a single event.data
                    # with \n between them.
                    content = event["value"]
                    lines = content.split("\n")
                    sse_payload = "\n".join(f"data: {line}" for line in lines)
                    yield f"{sse_payload}\n\n"

            yield f"data: [SESSION_ID]{session_id}\n\n"
            yield "data: [DONE]\n\n"

        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")