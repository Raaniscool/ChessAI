"""Puzzle Library: verified practice positions with metadata, stats and selection."""
from .index import PuzzleLibrary, get_puzzles
from .model import TYPES, Puzzle, from_example
from .select import Choice, Selection, select

__all__ = ["PuzzleLibrary", "get_puzzles", "TYPES", "Puzzle", "from_example", "Choice", "Selection", "select"]
