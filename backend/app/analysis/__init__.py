"""Game analysis engine (source-agnostic: it reads GameRecords, not PGN dialects).

    GameRecord -> analyzer.GameAnalyzer (python-chess replay + Stockfish + classify_move)
               -> motifs.detect / habits.detect_habits (Knowledge Library concept ids)
               -> weaknesses.recurring_weaknesses (evidence across games)
"""
from .analyzer import GameAnalyzer
from .weaknesses import recurring_weaknesses

__all__ = ["GameAnalyzer", "recurring_weaknesses"]
