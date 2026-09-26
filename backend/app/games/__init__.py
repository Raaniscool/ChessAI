"""Learner games: import (per-site importers) -> GameRecord -> private storage.

    Chess.com PGN importer (importers/chesscom.py)
        -> general game representation (model.GameRecord, validated by python-chess in pgn.py)
        -> game analysis engine (app/analysis)
"""
from .model import GameRecord
from .pgn import PgnError

__all__ = ["GameRecord", "PgnError"]
