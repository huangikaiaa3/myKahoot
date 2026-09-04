"""Exercise one complete 150-player quiz round against a running server."""

import argparse
import asyncio
import json
import time

import websockets


async def receive_type(socket, expected_type: str, timeout: float = 15) -> dict:
    """Read past status updates until the event this client is waiting for arrives."""
    while True:
        payload = json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))
        if payload["type"] == expected_type:
            return payload


async def connect_player(url: str, pin: str, number: int, semaphore: asyncio.Semaphore):
    async with semaphore:
        socket = await websockets.connect(url, open_timeout=15)
        await socket.send(json.dumps({"action": "join", "pin": pin, "name": f"Load Player {number:03d}"}))
        await receive_type(socket, "joined")
        return socket


async def run(url: str, player_count: int) -> None:
    started = time.perf_counter()
    host = await websockets.connect(url, open_timeout=15)
    players = []
    try:
        await host.send(json.dumps({"action": "host"}))
        await receive_type(host, "host_status")
        await host.send(json.dumps({"action": "reset"}))
        lobby = await receive_type(host, "host_status")

        join_started = time.perf_counter()
        semaphore = asyncio.Semaphore(25)
        players = await asyncio.gather(*(
            connect_player(url, lobby["pin"], number, semaphore)
            for number in range(1, player_count + 1)
        ))
        join_seconds = time.perf_counter() - join_started

        await host.send(json.dumps({"action": "next"}))
        questions = await asyncio.gather(*(receive_type(player, "question") for player in players))
        correct_answer = 1  # The first sample question's correct answer index.

        answer_started = time.perf_counter()
        await asyncio.gather(*(
            player.send(json.dumps({"action": "answer", "answer": correct_answer}))
            for player in players
        ))
        await asyncio.gather(*(receive_type(player, "answer_received") for player in players))
        answer_seconds = time.perf_counter() - answer_started

        await host.send(json.dumps({"action": "reveal"}))
        await asyncio.gather(*(receive_type(player, "reveal") for player in players))
        results = await asyncio.gather(*(receive_type(player, "round_result") for player in players))

        successful = sum(result["correct"] for result in results)
        elapsed = time.perf_counter() - started
        print(f"Players connected: {len(players)}/{player_count}")
        print(f"Join time: {join_seconds:.2f}s")
        print(f"Answer round-trip time: {answer_seconds:.2f}s")
        print(f"Question broadcasts received: {len(questions)}/{player_count}")
        print(f"Correct results received: {successful}/{player_count}")
        print(f"Total test time: {elapsed:.2f}s")
    finally:
        await asyncio.gather(*(player.close() for player in players), return_exceptions=True)
        await host.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws", help="WebSocket endpoint")
    parser.add_argument("--players", type=int, default=150, help="Number of simulated players")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.players))


if __name__ == "__main__":
    main()
