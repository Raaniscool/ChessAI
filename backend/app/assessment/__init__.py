"""Training (Puzzles tab -> Training): adaptive skill assessment from short games against a bot.

Lessons teach, Practice drills a chosen idea, Training *observes*. The learner gets a position from
a real or verified game, of the chosen phase, and plays a short segment (about 5-10 of their own
moves) against a bot near their level. The segment is then analysed by the same pipeline as
imported games and the evidence goes into a persistent, explainable skill profile that Practice
and Personalized read. Nothing about the position's idea is shown while playing.

    positions   where Training positions come from (opening trees, verified puzzle positions),
                their phase (analysis.analyzer.phase_of) and how the next one is chosen
    bot         the opponent: Stockfish's candidate moves, picked with a strength-dependent
                tolerance (no engine option, works with any AnalysisEngine)
    segment     one segment's state, its natural stopping point, the in-memory store
    evaluate    the analysis of a finished segment (analysis.analyzer.GameAnalyzer + motifs:
                validators decide concepts; a centipawn loss alone never names one)
    record      the persistent evidence in LearnerProfile.training
    needs       evidence -> per-concept and per-phase needs with confidence and reasons, shared
                by the Training feedback, Practice weighting and Personalized cards

Everything is deterministic given the segment's seed. Qwen is never involved.
"""
