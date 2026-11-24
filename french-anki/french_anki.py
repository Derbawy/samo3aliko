#!/usr/bin/env python3
"""French Anki pipeline: chunks_master.csv -> Francais_Sherif.apkg (with France-French audio).

Usage
-----
  python french_anki.py                      # rebuild the deck from the CSVs
  python french_anki.py --push               # rebuild, then load into Anki desktop via AnkiConnect
  python french_anki.py add "fr ; en ; tags [; note]" ...   # append chunks, then rebuild
  python french_anki.py add --file lesson.txt              # one "fr ; en ; tags" per line
  python french_anki.py add                                # paste lines, then Ctrl-D (Mac) / Ctrl-Z Enter (Windows)
  python french_anki.py add-pair "w1 ; w2 ; meaning1 ; meaning2 ; tip"

Design rules (do not change lightly -- they protect your review history):
  * chunks_master.csv is append-only. This script never deletes or rewrites rows.
  * Every deck/model ID below is fixed forever, so new builds merge into the same decks.
  * Each note's GUID is a hash of its (normalised) French text, so re-importing updates
    the note instead of duplicating it, and your review history is kept.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import genanki

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
CHUNKS_CSV = HERE / "chunks_master.csv"
PAIRS_CSV = HERE / "pairs_master.csv"
MEDIA_DIR = HERE / "media"
OUTPUT_APKG = HERE / "Francais_Sherif.apkg"

CHUNK_COLUMNS = ["french", "english", "tags", "date_added", "note"]
PAIR_COLUMNS = ["word1", "word2", "meaning1", "meaning2", "tip", "date_added"]

# --------------------------------------------------------------------------------------
# FIXED IDs -- never change these, or Anki will treat the deck as a brand-new one.
# --------------------------------------------------------------------------------------
ROOT_DECK = "Français – Sherif"  # note: en dash
DECK_IDS = {
    ROOT_DECK: 1135215982792,
    f"{ROOT_DECK}::Pharmacie": 1123630418348,
    f"{ROOT_DECK}::École": 1921453890482,
    f"{ROOT_DECK}::Aviation": 1154174216004,
    f"{ROOT_DECK}::La vie": 1841361769549,
    f"{ROOT_DECK}::Prononciation": 2128726266636,
}
MODEL_ID_CHUNK = 1585937399213
MODEL_ID_PAIRE = 1492339166531

# Tag (as typed in the CSV) -> subdeck. First match in this order wins when a chunk has
# several deck tags (e.g. "pharmacie la_vie" goes to Pharmacie).
TAG_TO_SUBDECK = [
    ("pharmacie", "Pharmacie"),
    ("ecole", "École"),
    ("aviation", "Aviation"),
    ("la_vie", "La vie"),
    ("prononciation", "Prononciation"),
]
# Accepted spellings -> canonical tag.
TAG_ALIASES = {
    "pharmacie": "pharmacie", "pharmacy": "pharmacie",
    "ecole": "ecole", "école": "ecole", "school": "ecole",
    "aviation": "aviation",
    "la_vie": "la_vie", "lavie": "la_vie", "la-vie": "la_vie", "vie": "la_vie", "life": "la_vie",
    "prononciation": "prononciation", "pronunciation": "prononciation",
    "shadow": "shadow",
}
SPECIAL_TAGS = {"shadow"}  # tags that change card generation but not the deck

# --------------------------------------------------------------------------------------
# Audio settings (France French). Changing these changes the cache key -> audio regenerates.
# --------------------------------------------------------------------------------------
TTS_LANG = "fr"
TTS_TLD = "fr"
TTS_PAUSE_SECONDS = 0.4  # be polite to Google between requests
TTS_RETRIES = 4


# ======================================================================================
# Text helpers
# ======================================================================================
def normalize_text(text: str) -> str:
    """NFC-normalise, convert non-breaking spaces, collapse whitespace, strip."""
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace(" ", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize_tags(raw: str) -> tuple[list[str], list[str]]:
    """Return (canonical_tags, unknown_tags). Unknown tags are kept (sanitised) but reported."""
    tags, unknown = [], []
    for tok in re.split(r"[\s,]+", (raw or "").strip()):
        if not tok:
            continue
        low = tok.lower()
        canon = TAG_ALIASES.get(low) or TAG_ALIASES.get(strip_accents(low))
        if canon is None:
            canon = re.sub(r"[^\w\-]", "_", strip_accents(low))
            unknown.append(tok)
        if canon not in tags:
            tags.append(canon)
    return tags, unknown


def chunk_guid(french: str) -> str:
    """Stable note GUID = hash of the normalised French text."""
    return genanki.guid_for("fr_chunk", normalize_text(french))


def pair_guid(word1: str, word2: str) -> str:
    return genanki.guid_for("fr_paire", normalize_text(word1), normalize_text(word2))


def subdeck_for(tags: Iterable[str]) -> str:
    tagset = set(tags)
    for tag, sub in TAG_TO_SUBDECK:
        if tag in tagset:
            return f"{ROOT_DECK}::{sub}"
    return ROOT_DECK


# ======================================================================================
# CSV storage (append-only)
# ======================================================================================
@dataclass
class Chunk:
    french: str
    english: str
    tags: list[str]
    date_added: str = ""
    note: str = ""
    line: int = 0

    @property
    def is_shadow(self) -> bool:
        return "shadow" in self.tags


@dataclass
class Pair:
    word1: str
    word2: str
    meaning1: str
    meaning2: str
    tip: str = ""
    date_added: str = ""


def _ensure_csv(path: Path, columns: list[str]) -> None:
    if not path.exists():
        # utf-8-sig (BOM) so Excel shows the accents correctly when you open the file.
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(columns)


def _append_rows(path: Path, columns: list[str], rows: list[dict]) -> None:
    """Append rows without touching existing content (adds a newline first if needed)."""
    _ensure_csv(path, columns)
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        needs_newline = False
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
    with path.open("a", encoding="utf-8", newline="") as f:
        if needs_newline:
            f.write("\r\n")
        w = csv.DictWriter(f, fieldnames=columns)
        for row in rows:
            w.writerow({c: row.get(c, "") for c in columns})


def _read_rows(path: Path, columns: list[str]) -> list[tuple[int, dict]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [c for c in columns if c not in header]
        if missing:
            raise SystemExit(f"{path.name}: missing column(s) {missing}. Expected header: {','.join(columns)}")
        out = []
        for row in reader:
            clean = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
            if not any(v.strip() for v in clean.values()):
                continue  # blank line
            out.append((reader.line_num, clean))
        return out


def load_chunks(path: Path = CHUNKS_CSV, warn: Callable[[str], None] = print) -> list[Chunk]:
    chunks, seen = [], {}
    for line, row in _read_rows(path, CHUNK_COLUMNS):
        french, english = normalize_text(row["french"]), normalize_text(row["english"])
        if not french or not english:
            warn(f"  ! {path.name} line {line}: needs both french and english -- skipped")
            continue
        tags, unknown = normalize_tags(row["tags"])
        if unknown:
            warn(f"  ! {path.name} line {line}: unknown tag(s) {unknown} -- kept as Anki tags (typo?)")
        guid = chunk_guid(french)
        if guid in seen:
            warn(f"  ! {path.name} line {line}: duplicate of line {seen[guid]} ({french!r}) -- skipped")
            continue
        seen[guid] = line
        chunks.append(Chunk(french, english, tags, row["date_added"].strip(), normalize_text(row["note"]), line))
    return chunks


def load_pairs(path: Path = PAIRS_CSV, warn: Callable[[str], None] = print) -> list[Pair]:
    pairs, seen = [], set()
    for line, row in _read_rows(path, PAIR_COLUMNS):
        p = Pair(*(normalize_text(row[c]) for c in PAIR_COLUMNS))
        if not (p.word1 and p.word2):
            warn(f"  ! {path.name} line {line}: needs word1 and word2 -- skipped")
            continue
        key = pair_guid(p.word1, p.word2)
        if key in seen:
            warn(f"  ! {path.name} line {line}: duplicate pair {p.word1}/{p.word2} -- skipped")
            continue
        seen.add(key)
        pairs.append(p)
    return pairs


def parse_chunk_line(text: str) -> dict | None:
    """'french ; english ; tags [; note]' -> row dict. Returns None for blank/comment lines."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    parts = [p.strip() for p in text.split(";", 3)]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Expected 'french ; english ; tags [; note]', got: {text!r}")
    parts += [""] * (4 - len(parts))
    return {"french": normalize_text(parts[0]), "english": normalize_text(parts[1]),
            "tags": " ".join(parts[2].split()), "note": normalize_text(parts[3])}


def add_chunks(lines: Iterable[str], path: Path = CHUNKS_CSV, today: str | None = None,
               log: Callable[[str], None] = print) -> tuple[int, int]:
    """Append new chunks; skip exact duplicates (same normalised French). Returns (added, skipped)."""
    today = today or dt.date.today().isoformat()
    existing = {chunk_guid(row["french"]) for _, row in _read_rows(path, CHUNK_COLUMNS)} if path.exists() else set()
    new_rows, skipped = [], 0
    for raw in lines:
        row = parse_chunk_line(raw)
        if row is None:
            continue
        g = chunk_guid(row["french"])
        if g in existing:
            log(f"  = already have: {row['french']}")
            skipped += 1
            continue
        _, unknown = normalize_tags(row["tags"])
        if unknown:
            log(f"  ! unknown tag(s) {unknown} on: {row['french']}")
        existing.add(g)
        row["date_added"] = today
        new_rows.append(row)
        log(f"  + {row['french']}")
    if new_rows:
        _append_rows(path, CHUNK_COLUMNS, new_rows)
    return len(new_rows), skipped


def add_pairs(lines: Iterable[str], path: Path = PAIRS_CSV, today: str | None = None,
              log: Callable[[str], None] = print) -> tuple[int, int]:
    today = today or dt.date.today().isoformat()
    existing = {pair_guid(r["word1"], r["word2"]) for _, r in _read_rows(path, PAIR_COLUMNS)} if path.exists() else set()
    new_rows, skipped = [], 0
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [normalize_text(p) for p in raw.split(";", 4)]
        if len(parts) < 4:
            raise ValueError(f"Expected 'word1 ; word2 ; meaning1 ; meaning2 [; tip]', got: {raw!r}")
        parts += [""] * (5 - len(parts))
        g = pair_guid(parts[0], parts[1])
        if g in existing:
            log(f"  = already have: {parts[0]} / {parts[1]}")
            skipped += 1
            continue
        existing.add(g)
        new_rows.append(dict(zip(PAIR_COLUMNS, parts + [today])))
        log(f"  + {parts[0]} / {parts[1]}")
    if new_rows:
        _append_rows(path, PAIR_COLUMNS, new_rows)
    return len(new_rows), skipped


# ======================================================================================
# Audio (gTTS, France French) with a content-addressed cache
# ======================================================================================
def audio_filename(text: str, slow: bool) -> str:
    key = f"{normalize_text(text)}|lang={TTS_LANG}|tld={TTS_TLD}|slow={slow}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"frs_{digest}_{'s' if slow else 'n'}.mp3"


def gtts_synthesize(text: str, slow: bool, dest: Path) -> None:
    from gtts import gTTS  # imported lazily so tests / cached builds don't need the network

    gTTS(text=text, lang=TTS_LANG, tld=TTS_TLD, slow=slow).save(str(dest))


class AudioCache:
    def __init__(self, media_dir: Path = MEDIA_DIR,
                 synthesize: Callable[[str, bool, Path], None] = gtts_synthesize,
                 pause: float = TTS_PAUSE_SECONDS, retries: int = TTS_RETRIES):
        self.media_dir = media_dir
        self.synthesize = synthesize
        self.pause = pause
        self.retries = retries
        self.generated = 0
        self.cached = 0
        self.failed: list[str] = []
        media_dir.mkdir(parents=True, exist_ok=True)

    def get(self, text: str, slow: bool) -> Path | None:
        """Return the path to the MP3 for text, generating it once. None if generation failed."""
        path = self.media_dir / audio_filename(text, slow)
        if path.exists() and path.stat().st_size > 0:
            self.cached += 1
            return path
        tmp = path.with_suffix(".part")
        for attempt in range(1, self.retries + 1):
            try:
                self.synthesize(text, slow, tmp)
                if not tmp.exists() or tmp.stat().st_size == 0:
                    raise RuntimeError("empty audio file")
                tmp.replace(path)  # atomic: never leaves a half-written MP3 in the cache
                self.generated += 1
                time.sleep(self.pause)
                return path
            except Exception as e:  # network errors, rate limits (429), ...
                if attempt == self.retries:
                    self.failed.append(f"{text!r} (slow={slow}): {e}")
                    tmp.unlink(missing_ok=True)
                    return None
                time.sleep(self.pause * 2 ** attempt)
        return None


def sound(path: Path | None) -> str:
    return f"[sound:{path.name}]" if path else ""


# ======================================================================================
# Note types
# ======================================================================================
CSS = r"""
.card {
  --bg: #faf8f3; --fg: #1c1c1e; --muted: #6e6e73; --accent: #2456b3;
  --chip: #e8eefb; --line: #dedad2; --good: #1f7a4d; --note-bg: #f1eee6;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 22px; line-height: 1.45; text-align: center;
  color: var(--fg); background: var(--bg);
  padding: 28px 16px 40px; max-width: 640px; margin: 0 auto;
}
.nightMode, .night_mode, .nightMode .card, .night_mode .card, .card.nightMode, .card.night_mode {
  --bg: #1c1c1e; --fg: #f2f2f7; --muted: #a1a1a6; --accent: #8fb3ff;
  --chip: #2c3348; --line: #3a3a3c; --good: #6fd19c; --note-bg: #2a2a2c;
  background: var(--bg); color: var(--fg);
}
.kind { display: inline-block; font-size: 13px; letter-spacing: .08em; text-transform: uppercase;
        color: var(--accent); background: var(--chip); border-radius: 999px; padding: 4px 12px; margin-bottom: 22px; }
.kind + .audio, .audio { display: block; margin: 0 0 6px; }
.prompt { color: var(--muted); font-size: 19px; margin: 18px 0 8px; }
.big { font-size: 30px; font-weight: 600; color: var(--fg); }
.fr { font-size: 30px; font-weight: 650; line-height: 1.3; margin: 10px 0 12px; }
.en { font-size: 21px; color: var(--muted); margin: 6px 0; }
.note { font-size: 17px; background: var(--note-bg); border-radius: 12px; padding: 10px 14px;
        margin: 18px auto 0; max-width: 520px; text-align: left; }
.btns { margin-top: 18px; display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }
.play { font: inherit; font-size: 18px; color: var(--accent); background: var(--chip);
        border: 1px solid var(--line); border-radius: 12px; padding: 10px 18px; cursor: pointer; }
.play:active { transform: scale(.97); }
hr#answer { border: none; border-top: 1px solid var(--line); margin: 22px 0; }
.choices { display: flex; justify-content: center; align-items: center; gap: 16px; margin-top: 18px; }
.choices .w { font-size: 30px; font-weight: 650; padding: 8px 18px; border: 1px solid var(--line); border-radius: 12px; }
.choices .or { color: var(--muted); font-size: 18px; }
.verdict { font-size: 34px; font-weight: 700; color: var(--good); margin-bottom: 14px; }
table.pair { margin: 8px auto 0; border-collapse: collapse; font-size: 20px; }
table.pair td { padding: 8px 12px; border-bottom: 1px solid var(--line); text-align: left; }
table.pair td.w { font-weight: 650; font-size: 24px; }
table.pair tr.hit td.w { color: var(--good); }
.tip { font-size: 17px; text-align: left; background: var(--note-bg); border-radius: 12px;
       padding: 12px 14px; margin: 20px auto 0; max-width: 520px; }
.replay-button svg { width: 44px; height: 44px; }  /* bigger tap target for Anki's own play buttons */
"""

# Plays a media file *without* autoplay (Anki autoplays every [sound:] tag, so the
# "vitesse normale" button on the LISTEN front uses HTML5 audio instead).
PLAY_JS = """<script>
function frsPlay(src){try{if(window.__frsAudio){window.__frsAudio.pause();}
window.__frsAudio=new Audio(src);window.__frsAudio.play();}catch(e){}}
</script>"""


def _btn(field_name: str, label: str) -> str:
    return (f'{{{{#{field_name}}}}}<button class="play" onclick="frsPlay(\'{{{{{field_name}}}}}\')">'
            f"{label}</button>{{{{/{field_name}}}}}")


CHUNK_FIELDS = [
    "French", "English", "AudioNormal", "AudioSlow", "Tags", "Note",
    # Helper fields (filled automatically, not shown on cards):
    "Shadow",           # "1" when the chunk is tagged shadow -> creates the SHADOW card
    "AudioNormalFile",  # bare filename of the normal-speed MP3, for the non-autoplay button
]

LISTEN_Q = f"""<div class="kind">🎧 Écoute</div>
<div class="audio">{{{{AudioSlow}}}}</div>
<div class="prompt">Écoutez et répétez</div>
<div class="btns">{_btn("AudioNormalFile", "▶ Vitesse normale")}</div>
{PLAY_JS}"""

LISTEN_A = f"""<div class="kind">🎧 Écoute</div>
<div class="fr">{{{{French}}}}</div>
<div class="en">{{{{English}}}}</div>
{{{{#Note}}}}<div class="note">{{{{Note}}}}</div>{{{{/Note}}}}
<div class="btns">{_btn("AudioNormalFile", "▶ Réécouter")}</div>
{PLAY_JS}"""

SPEAK_Q = """<div class="kind">🗣️ Parlez</div>
<div class="big">{{English}}</div>
<div class="prompt">Dites-le à voix haute, puis retournez la carte.</div>"""

SPEAK_A = """{{FrontSide}}
<hr id="answer">
<div class="fr">{{French}}</div>
{{AudioNormal}}
{{#Note}}<div class="note">{{Note}}</div>{{/Note}}"""

SHADOW_Q = """{{#Shadow}}<div class="kind">🔁 Shadowing</div>
<div class="audio">{{AudioNormal}}</div>
<div class="fr">{{French}}</div>
<div class="prompt">Écoutez et parlez en même temps.</div>{{/Shadow}}"""

SHADOW_A = """{{#Shadow}}<div class="kind">🔁 Shadowing</div>
<div class="fr">{{French}}</div>
<div class="big">Répétez 3 fois</div>
{{AudioNormal}}
<div class="en">{{English}}</div>{{/Shadow}}"""

CHUNK_MODEL = genanki.Model(
    MODEL_ID_CHUNK,
    "FR Chunk",
    fields=[{"name": n, "font": "Arial"} for n in CHUNK_FIELDS],
    templates=[
        {"name": "1 Listen", "qfmt": LISTEN_Q, "afmt": LISTEN_A},
        {"name": "2 Speak", "qfmt": SPEAK_Q, "afmt": SPEAK_A},
        {"name": "3 Shadow", "qfmt": SHADOW_Q, "afmt": SHADOW_A},
    ],
    css=CSS,
    sort_field_index=0,
)

PAIRE_FIELDS = ["Word1", "Word2", "Meaning1", "Meaning2", "Audio1", "Audio2", "Tip"]


def _paire_q(audio_field: str) -> str:
    return f"""<div class="kind">👂 Paire minimale</div>
<div class="audio">{{{{{audio_field}}}}}</div>
<div class="prompt">Quel mot entendez-vous ?</div>
<div class="choices"><span class="w">{{{{Word1}}}}</span><span class="or">ou</span><span class="w">{{{{Word2}}}}</span></div>"""


def _paire_a(hit: int) -> str:
    rows = []
    for i in (1, 2):
        cls = ' class="hit"' if i == hit else ""
        mark = "✓ " if i == hit else ""
        rows.append(f'<tr{cls}><td class="w">{mark}{{{{Word{i}}}}}</td><td>{{{{Meaning{i}}}}}</td><td>{{{{Audio{i}}}}}</td></tr>')
    return f"""<div class="kind">👂 Paire minimale</div>
<div class="verdict">{{{{Word{hit}}}}}</div>
<table class="pair">{''.join(rows)}</table>
{{{{#Tip}}}}<div class="tip">💡 {{{{Tip}}}}</div>{{{{/Tip}}}}"""


PAIRE_MODEL = genanki.Model(
    MODEL_ID_PAIRE,
    "FR Paire",
    fields=[{"name": n, "font": "Arial"} for n in PAIRE_FIELDS],
    templates=[
        {"name": "Mot 1", "qfmt": _paire_q("Audio1"), "afmt": _paire_a(1)},
        {"name": "Mot 2", "qfmt": _paire_q("Audio2"), "afmt": _paire_a(2)},
    ],
    css=CSS,
    sort_field_index=0,
)


# ======================================================================================
# Build
# ======================================================================================
DECK_DESCRIPTION = (
    "Chunks from my French lessons (standard French, France audio).<br>"
    "🎧 <b>Écoute</b>: listen (slow first), repeat, then flip.<br>"
    "🗣️ <b>Parlez</b>: say the French aloud <i>before</i> flipping.<br>"
    "🔁 <b>Shadowing</b>: speak along with the audio, then repeat 3×.<br>"
    "👂 <b>Paire minimale</b>: which word did you hear?"
)


@dataclass
class BuildResult:
    apkg: Path
    notes: int = 0
    cards: dict = field(default_factory=dict)
    per_deck: dict = field(default_factory=dict)
    audio_generated: int = 0
    audio_cached: int = 0
    audio_failed: list = field(default_factory=list)


def _html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build(chunks_csv: Path = CHUNKS_CSV, pairs_csv: Path = PAIRS_CSV, out: Path = OUTPUT_APKG,
          audio: AudioCache | None = None, allow_missing_audio: bool = False,
          log: Callable[[str], None] = print) -> BuildResult:
    audio = audio or AudioCache()
    chunks = load_chunks(chunks_csv, warn=log)
    pairs = load_pairs(pairs_csv, warn=log)

    decks = {name: genanki.Deck(did, name, description=DECK_DESCRIPTION if name == ROOT_DECK else "")
             for name, did in DECK_IDS.items()}
    media: set[Path] = set()
    res = BuildResult(apkg=out, cards={"Listen": 0, "Speak": 0, "Shadow": 0, "Paire": 0})

    total = 2 * len(chunks) + 2 * len(pairs)
    log(f"Audio: checking {total} files (new ones are downloaded from Google TTS, France voice)...")

    for i, ch in enumerate(chunks):
        normal, slow = audio.get(ch.french, slow=False), audio.get(ch.french, slow=True)
        media.update(p for p in (normal, slow) if p)
        note = genanki.Note(
            model=CHUNK_MODEL,
            fields=[_html(ch.french), _html(ch.english), sound(normal), sound(slow),
                    " ".join(ch.tags), _html(ch.note),
                    "1" if ch.is_shadow else "", normal.name if normal else ""],
            tags=["fr_chunk"] + ch.tags,
            guid=chunk_guid(ch.french),
            due=i,  # new cards are introduced in the order you added them
        )
        deck = subdeck_for(ch.tags)
        decks[deck].add_note(note)
        res.notes += 1
        res.cards["Listen"] += 1 if (normal or slow) else 0
        res.cards["Speak"] += 1
        res.cards["Shadow"] += 1 if ch.is_shadow else 0
        res.per_deck[deck] = res.per_deck.get(deck, 0) + 1

    pron = f"{ROOT_DECK}::Prononciation"
    for j, p in enumerate(pairs):
        a1, a2 = audio.get(p.word1, slow=False), audio.get(p.word2, slow=False)
        media.update(x for x in (a1, a2) if x)
        note = genanki.Note(
            model=PAIRE_MODEL,
            fields=[_html(p.word1), _html(p.word2), _html(p.meaning1), _html(p.meaning2),
                    sound(a1), sound(a2), _html(p.tip)],
            tags=["fr_paire", "prononciation"],
            guid=pair_guid(p.word1, p.word2),
            due=len(chunks) + j,
        )
        decks[pron].add_note(note)
        res.notes += 1
        res.cards["Paire"] += 2
        res.per_deck[pron] = res.per_deck.get(pron, 0) + 1

    res.audio_generated, res.audio_cached, res.audio_failed = audio.generated, audio.cached, audio.failed
    if audio.failed and not allow_missing_audio:
        for f in audio.failed:
            log(f"  ✗ audio failed: {f}")
        raise SystemExit(
            f"{len(audio.failed)} audio file(s) could not be generated (are you online?). Nothing was written.\n"
            "Fix the connection and re-run, or use --allow-missing-audio to build without them."
        )

    pkg = genanki.Package(list(decks.values()))
    pkg.media_files = sorted(str(p) for p in media)
    tmp = out.with_suffix(".apkg.part")
    pkg.write_to_file(str(tmp))
    tmp.replace(out)
    return res


def print_summary(res: BuildResult, log: Callable[[str], None] = print) -> None:
    c = res.cards
    log(f"\n✓ Wrote {res.apkg.name}: {res.notes} notes, {sum(c.values())} cards "
        f"(Écoute {c['Listen']}, Parlez {c['Speak']}, Shadowing {c['Shadow']}, Paires {c['Paire']})")
    for deck in DECK_IDS:
        if deck in res.per_deck:
            log(f"    {deck}: {res.per_deck[deck]} notes")
    log(f"  Audio: {res.audio_generated} new, {res.audio_cached} from cache"
        + (f", {len(res.audio_failed)} FAILED" if res.audio_failed else ""))


# ======================================================================================
# AnkiConnect (optional direct push)
# ======================================================================================
ANKICONNECT_URL = "http://127.0.0.1:8765"


def anki_connect(action: str, url: str = ANKICONNECT_URL, timeout: float = 60, **params):
    body = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.loads(r.read().decode("utf-8"))
    if reply.get("error"):
        raise RuntimeError(f"AnkiConnect {action}: {reply['error']}")
    return reply.get("result")


def push_to_anki(apkg: Path, url: str = ANKICONNECT_URL, sync: bool = True,
                 log: Callable[[str], None] = print) -> bool:
    """Import the .apkg into the running Anki desktop via AnkiConnect.

    Importing the package (rather than addNote/updateNote) keeps the stable GUIDs, so a
    pushed note and a later manually imported .apkg never duplicate each other.
    Returns False (and explains the manual import) if AnkiConnect isn't reachable.
    """
    try:
        version = anki_connect("version", url=url, timeout=3)
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError, RuntimeError, ValueError):
        log("\nAnkiConnect isn't reachable (is Anki desktop open with add-on 2055492159?).\n"
            f"Fallback: in Anki, File → Import… → {apkg}")
        return False
    log(f"\nAnkiConnect v{version} found -- importing…")
    anki_connect("importPackage", url=url, path=str(apkg.resolve()))
    log("✓ Imported into Anki desktop.")
    if sync:
        try:
            anki_connect("sync", url=url, timeout=120)
            log("✓ Synced to AnkiWeb (your phone gets it on its next sync).")
        except Exception as e:  # not logged in, offline, ...
            log(f"  (Sync skipped: {e}. Click Sync in Anki when ready.)")
    return True


# ======================================================================================
# CLI
# ======================================================================================
def _read_input_lines(items: list[str], file: str | None) -> list[str]:
    if file:
        return Path(file).read_text(encoding="utf-8-sig").splitlines()
    if items:
        return items
    if sys.stdin.isatty():
        print("Paste lines like:  Ça marche. ; Sounds good. ; la_vie shadow")
        print("Then press Ctrl-D (Mac/Linux) or Ctrl-Z then Enter (Windows).")
    return sys.stdin.read().splitlines()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles + accents/emoji
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Build the 'Français – Sherif' Anki deck.")
    ap.add_argument("--push", action="store_true", help="also import into Anki desktop via AnkiConnect")
    ap.add_argument("--no-sync", action="store_true", help="with --push: don't trigger an AnkiWeb sync")
    ap.add_argument("--allow-missing-audio", action="store_true", help="build even if some audio fails")
    sub = ap.add_subparsers(dest="cmd")

    p_build = sub.add_parser("build", help="rebuild the deck (default)")
    p_add = sub.add_parser("add", help="append chunks ('fr ; en ; tags [; note]') then rebuild")
    p_add.add_argument("items", nargs="*")
    p_add.add_argument("--file", "-f")
    p_add.add_argument("--no-build", action="store_true")
    p_pair = sub.add_parser("add-pair", help="append minimal pairs ('w1 ; w2 ; m1 ; m2 ; tip') then rebuild")
    p_pair.add_argument("items", nargs="*")
    p_pair.add_argument("--file", "-f")
    p_pair.add_argument("--no-build", action="store_true")
    for p in (p_build, p_add, p_pair):  # allow flags after the subcommand too
        p.add_argument("--push", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--no-sync", action="store_true", default=argparse.SUPPRESS)
        p.add_argument("--allow-missing-audio", action="store_true", default=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.cmd in ("add", "add-pair"):
        lines = _read_input_lines(args.items, args.file)
        try:
            fn, target = (add_chunks, CHUNKS_CSV) if args.cmd == "add" else (add_pairs, PAIRS_CSV)
            added, skipped = fn(lines)
        except ValueError as e:
            print(f"✗ {e}")
            return 2
        print(f"{target.name}: {added} added, {skipped} duplicate(s) skipped.")
        if args.no_build:
            return 0

    res = build(allow_missing_audio=args.allow_missing_audio)
    print_summary(res)
    if args.push:
        push_to_anki(res.apkg, sync=not args.no_sync)
    else:
        print(f"\nNext: in Anki desktop, File → Import… → {res.apkg}  (or re-run with --push)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
