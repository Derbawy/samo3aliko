# Français – Sherif · Anki pipeline

1. One-time setup: install Python 3, then run `pip install -r requirements.txt` in this folder.
2. After each lesson, add your chunks: `python french_anki.py add`, paste lines like `Ça marche. ; Sounds good. ; la_vie shadow` (optional 4th part = note), then press Ctrl-D (Mac) or Ctrl-Z + Enter (Windows).
3. That appends to `chunks_master.csv` (duplicates skipped, nothing ever deleted) and rebuilds `Francais_Sherif.apkg`, with new France-French audio downloaded once and cached in `media/`.
4. Import it into Anki desktop with File → Import → `Francais_Sherif.apkg` (or add `--push` with AnkiConnect running), then click Sync so your phone gets it.
5. Tags: pharmacie · ecole · aviation · la_vie (picks the subdeck) + shadow (adds a Shadowing card). Minimal pairs: `python french_anki.py add-pair "w1 ; w2 ; meaning1 ; meaning2 ; tip"`.
