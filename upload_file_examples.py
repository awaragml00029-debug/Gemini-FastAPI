#!/usr/bin/env python3
"""Examples for sending files to Gemini-FastAPI.

The API does not expose a multipart upload endpoint. File-like inputs are sent
inside JSON request bodies, either as URLs/data URLs or as raw base64 strings.
"""

import base64
import os
from pathlib import Path

import requests

BASE_URL = os.getenv("GEMINI_FASTAPI_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("GEMINI_FASTAPI_API_KEY", "")
MODEL = os.getenv("GEMINI_FASTAPI_MODEL", "gemini-2.5-pro")

HEADERS = {"Content-Type": "application/json"}
if API_KEY:
    HEADERS["Authorization"] = f"Bearer {API_KEY}"


def read_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def post_json(path: str, payload: dict) -> dict:
    response = requests.post(f"{BASE_URL}{path}", headers=HEADERS, json=payload, timeout=120)
    response.raise_for_status()
    return response.json()


def chat_with_image_url() -> dict:
    return post_json(
        "/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/image.png"
                            },
                        },
                    ],
                }
            ],
        },
    )


def chat_with_image_data_url(image_path: str) -> dict:
    data_url = f"data:image/png;base64,{read_base64(image_path)}"
    return post_json(
        "/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this local image."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        },
    )


def chat_with_file_base64(file_path: str) -> dict:
    return post_json(
        "/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this file."},
                        {
                            "type": "file",
                            "file": {
                                "filename": Path(file_path).name,
                                "file_data": read_base64(file_path),
                            },
                        },
                    ],
                }
            ],
        },
    )


def chat_with_audio_base64(audio_path: str) -> dict:
    return post_json(
        "/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe or summarize this audio."},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": read_base64(audio_path),
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
        },
    )


def responses_with_file_base64(file_path: str) -> dict:
    return post_json(
        "/v1/responses",
        {
            "model": MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Summarize this file."},
                        {
                            "type": "input_file",
                            "filename": Path(file_path).name,
                            "file_data": read_base64(file_path),
                        },
                    ],
                }
            ],
        },
    )


def responses_with_file_url() -> dict:
    return post_json(
        "/v1/responses",
        {
            "model": MODEL,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Summarize this remote file."},
                        {
                            "type": "input_file",
                            "filename": "example.pdf",
                            "file_url": "https://example.com/example.pdf",
                        },
                    ],
                }
            ],
        },
    )


if __name__ == "__main__":
    print("Set GEMINI_FASTAPI_BASE_URL, GEMINI_FASTAPI_API_KEY, and GEMINI_FASTAPI_MODEL.")
    print("Call one of the example functions with your local file path.")
