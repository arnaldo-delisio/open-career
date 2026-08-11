"""Prompt assets. Prompts live here as files, loaded by name, so they ship in
the wheel with the package and stay reviewable as text."""

from pathlib import Path


def load_prompt(name: str) -> str:
    return (Path(__file__).parent / name).read_text()
