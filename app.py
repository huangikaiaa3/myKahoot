import asyncio
import json
import os
import re
import secrets
import time
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qrcode
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from qrcode.image.svg import SvgPathImage


ROOT = Path(__file__).parent
QUESTIONS_PATH = ROOT / "data" / "questions.json"
QUESTION_IMAGES_PATH = ROOT / "data" / "question-images"
ADMIN_PAGE = ROOT / "admin" / "questions.html"
security = HTTPBasic()
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
QUESTION_IMAGE_URL_PATTERN = re.compile(r"^/question-images/[A-Za-z0-9_-]+\.(?:jpg|png|gif|webp)$")

class QuestionInput(BaseModel):
    prompt: str
    answers: list[str]
    correct: int
    time_limit: int
    description: str | None = None
    image_url: str | None = None


class QuestionsInput(BaseModel):
    questions: list[QuestionInput]


def validate_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not questions:
        raise ValueError("至少要保留一題。")
    validated = []
    for index, question in enumerate(questions, start=1):
        prompt = str(question.get("prompt", "")).strip()
        answers = [str(answer).strip() for answer in question.get("answers", [])]
        correct = question.get("correct")
        time_limit = question.get("time_limit")
        description = str(question.get("description") or "").strip()
        image_url = str(question.get("image_url") or "").strip()
        if not prompt or len(answers) < 2 or any(not answer for answer in answers):
            raise ValueError(f"第 {index} 題需要題目與至少兩個完整選項。")
        if not isinstance(correct, int) or correct not in range(len(answers)):
            raise ValueError(f"第 {index} 題的正確答案設定無效。")
        if not isinstance(time_limit, int) or not 5 <= time_limit <= 120:
            raise ValueError(f"第 {index} 題的作答時間必須介於 5 到 120 秒。")
        if len(description) > 1_000:
            raise ValueError(f"第 {index} 題的說明不可超過 1000 個字。")
        if image_url and not QUESTION_IMAGE_URL_PATTERN.fullmatch(image_url):
            raise ValueError(f"第 {index} 題的圖片網址無效。")
        validated.append({
            "prompt": prompt,
            "answers": answers,
            "correct": correct,
            "time_limit": time_limit,
            **({"description": description} if description else {}),
            **({"image_url": image_url} if image_url else {}),
        })
    return validated


def load_questions() -> list[dict[str, Any]]:
    try:
        return validate_questions(json.loads(QUESTIONS_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise RuntimeError(f"找不到題庫檔案：{QUESTIONS_PATH}") from error
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法載入題庫檔案：{QUESTIONS_PATH}") from error


def save_questions(questions: list[dict[str, Any]]) -> None:
    QUESTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = QUESTIONS_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(QUESTIONS_PATH)


def require_questions_password(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    password = os.environ.get("QUESTIONS_ADMIN_PASSWORD")
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="尚未設定 QUESTIONS_ADMIN_PASSWORD。",
        )
    if not secrets.compare_digest(credentials.password, password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理密碼錯誤。",
            headers={"WWW-Authenticate": "Basic"},
        )


QUIZ = load_questions()


@dataclass
class Player:
    id: str
    name: str
    score: int = 0
    answer: int | None = None
    answered_at: float | None = None
    question_points: int = 0
    socket: WebSocket | None = None


@dataclass
class Game:
    pin: str = field(default_factory=lambda: f"{secrets.randbelow(1_000_000):06d}")
    players: dict[str, Player] = field(default_factory=dict)
    host: WebSocket | None = None
    phase: str = "lobby"  # lobby, question, reveal, podium
    question_index: int = -1
    question_started_at: float | None = None
    remaining: int = 0
    timer_task: asyncio.Task[None] | None = None


game = Game()
app = FastAPI(title="火花問答")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
QUESTION_IMAGES_PATH.mkdir(parents=True, exist_ok=True)
app.mount("/question-images", StaticFiles(directory=QUESTION_IMAGES_PATH), name="question-images")


@app.middleware("http")
async def prevent_stale_frontend(request: Request, call_next):
    """Always serve the latest client assets during this one-off event build."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def question_payload() -> dict[str, Any]:
    question = QUIZ[game.question_index]
    return {
        "type": "question",
        "index": game.question_index,
        "total": len(QUIZ),
        "prompt": question["prompt"],
        "answers": question["answers"],
        "time_limit": question["time_limit"],
        "started_at": game.question_started_at,
        "remaining": game.remaining,
    }


def standings(limit: int = 10) -> list[dict[str, Any]]:
    ranked = sorted(game.players.values(), key=lambda player: (-player.score, player.name.lower()))
    return [
        {"rank": index + 1, "name": player.name, "score": player.score}
        for index, player in enumerate(ranked[:limit])
    ]


async def send(socket: WebSocket | None, payload: dict[str, Any]) -> None:
    if socket:
        try:
            await socket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            pass


async def broadcast(payload: dict[str, Any]) -> None:
    sockets = [player.socket for player in game.players.values() if player.socket]
    if game.host:
        sockets.append(game.host)
    await asyncio.gather(*(send(socket, payload) for socket in sockets), return_exceptions=True)


async def host_status() -> None:
    payload = {
        "type": "host_status",
        "pin": game.pin,
        "phase": game.phase,
        "players": len(game.players),
        "question_index": game.question_index,
        "total": len(QUIZ),
        "standings": standings(),
    }
    if game.question_index >= 0:
        payload["current_question"] = question_payload()
        if game.phase == "reveal":
            question = QUIZ[game.question_index]
            payload["current_question"].update({
                "description": question.get("description"),
                "image_url": question.get("image_url"),
            })
    await send(game.host, payload)


async def show_question() -> None:
    cancel_timer()
    game.question_index += 1
    game.phase = "question"
    game.question_started_at = time.time()
    game.remaining = QUIZ[game.question_index]["time_limit"]
    for player in game.players.values():
        player.answer = None
        player.answered_at = None
        player.question_points = 0
    await broadcast(question_payload())
    await host_status()
    game.timer_task = asyncio.create_task(question_timer(game.question_index, game.question_started_at))


def cancel_timer() -> None:
    task = game.timer_task
    game.timer_task = None
    if task and task is not asyncio.current_task():
        task.cancel()


async def question_timer(question_index: int, started_at: float | None) -> None:
    if started_at is None:
        return
    deadline = started_at + QUIZ[question_index]["time_limit"]
    last_remaining: int | None = None
    while game.phase == "question" and game.question_index == question_index:
        remaining = max(0, int(deadline - time.time() + 0.999))
        if remaining != last_remaining:
            game.remaining = remaining
            await broadcast({"type": "timer", "remaining": remaining})
            last_remaining = remaining
        if remaining == 0:
            await reveal()
            return
        await asyncio.sleep(0.1)


async def reveal() -> None:
    if game.phase != "question":
        return
    game.phase = "reveal"
    cancel_timer()
    question = QUIZ[game.question_index]
    answered = sum(player.answer is not None for player in game.players.values())
    payload = {
        "type": "reveal",
        "correct": question["correct"],
        "description": question.get("description"),
        "image_url": question.get("image_url"),
        "standings": standings(),
        "answered": answered,
        "players": len(game.players),
    }
    await broadcast(payload)
    ranked_players = sorted(game.players.values(), key=lambda player: (-player.score, player.name.lower()))
    ranks = {player.id: index + 1 for index, player in enumerate(ranked_players)}
    await asyncio.gather(*(
        send(player.socket, {
            "type": "round_result",
            "correct": player.answer == question["correct"],
            "points": player.question_points,
            "total_score": player.score,
            "rank": ranks[player.id],
        })
        for player in game.players.values()
    ), return_exceptions=True)
    await host_status()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/questions", dependencies=[Depends(require_questions_password)])
async def questions_editor() -> FileResponse:
    return FileResponse(ADMIN_PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/questions", dependencies=[Depends(require_questions_password)])
async def get_questions() -> dict[str, list[dict[str, Any]]]:
    return {"questions": QUIZ}


@app.put("/api/questions", dependencies=[Depends(require_questions_password)])
async def update_questions(payload: QuestionsInput) -> dict[str, list[dict[str, Any]]]:
    if game.phase != "lobby":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="遊戲進行中，無法修改題目。")
    try:
        updated_questions = validate_questions([question.model_dump() for question in payload.questions])
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    QUIZ[:] = updated_questions
    save_questions(QUIZ)
    return {"questions": QUIZ}


@app.post("/api/question-images", dependencies=[Depends(require_questions_password)])
async def upload_question_image(image: UploadFile = File(...)) -> dict[str, str]:
    suffix = IMAGE_TYPES.get(image.content_type or "")
    if not suffix:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="僅支援 JPG、PNG、GIF 或 WebP 圖片。")

    content = await image.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="請選擇圖片檔案。")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="圖片不可超過 5 MB。")

    filename = f"{secrets.token_urlsafe(18)}{suffix}"
    (QUESTION_IMAGES_PATH / filename).write_bytes(content)
    return {"image_url": f"/question-images/{filename}"}


@app.get("/join-qr.svg")
async def join_qr(request: Request, url: str) -> Response:
    """Return a compact QR code for the exact public URL the host is using."""
    if not url.startswith(("http://", "https://")):
        return Response("網址無效", status_code=400)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buffer)
    return Response(buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket) -> None:
    await socket.accept()
    player_id: str | None = None
    is_host = False
    try:
        while True:
            message = json.loads(await socket.receive_text())
            action = message.get("action")
            if action == "host":
                is_host = True
                game.host = socket
                await host_status()
            elif action == "join":
                name = str(message.get("name", "")).strip()[:24]
                if message.get("pin") != game.pin or not name:
                    await send(socket, {"type": "error", "message": "請確認遊戲 PIN 與名稱。"})
                    continue
                if game.phase != "lobby":
                    await send(socket, {"type": "error", "message": "遊戲已經開始。"})
                    continue
                player_id = secrets.token_urlsafe(12)
                game.players[player_id] = Player(id=player_id, name=name, socket=socket)
                await send(socket, {"type": "joined", "name": name, "pin": game.pin})
                await host_status()
            elif action == "next" and is_host:
                if game.phase == "lobby" or (game.phase == "reveal" and game.question_index < len(QUIZ) - 1):
                    await show_question()
                elif game.phase == "reveal":
                    game.phase = "podium"
                    await broadcast({"type": "podium", "standings": standings()})
                    await host_status()
            elif action == "reveal" and is_host:
                await reveal()
            elif action == "answer" and player_id and game.phase == "question":
                player = game.players[player_id]
                if player.answer is not None:
                    continue
                answer = message.get("answer")
                question = QUIZ[game.question_index]
                if not isinstance(answer, int) or answer not in range(len(question["answers"])):
                    continue
                player.answer = answer
                player.answered_at = time.time()
                points = 0
                if answer == question["correct"]:
                    elapsed = player.answered_at - (game.question_started_at or player.answered_at)
                    speed_bonus = max(0, int((question["time_limit"] - elapsed) * 25))
                    points = 500 + speed_bonus
                    player.score += points
                player.question_points = points
                await send(socket, {"type": "answer_received"})
                await host_status()
            elif action == "reset" and is_host:
                cancel_timer()
                game.players.clear()
                game.phase = "lobby"
                game.question_index = -1
                game.question_started_at = None
                game.remaining = 0
                game.pin = f"{secrets.randbelow(1_000_000):06d}"
                await host_status()
            elif action == "restart" and is_host:
                cancel_timer()
                game.phase = "lobby"
                game.question_index = -1
                game.question_started_at = None
                game.remaining = 0
                for player in game.players.values():
                    player.score = 0
                    player.answer = None
                    player.answered_at = None
                    player.question_points = 0
                await broadcast({"type": "restarted"})
                await host_status()
    except WebSocketDisconnect:
        pass
    finally:
        if is_host and game.host is socket:
            game.host = None
        if player_id and player_id in game.players:
            game.players[player_id].socket = None
        await host_status()
