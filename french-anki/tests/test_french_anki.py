"""Tests for french_anki.py.

Run:  python -m pytest french-anki/tests -q
The import tests need the official `anki` package (pip install anki); they're skipped otherwise.
Audio is faked (no network) so tests are fast and deterministic.
"""

import http.server
import json
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import french_anki as fa  # noqa: E402

anki = pytest.importorskip("anki")
from anki.collection import Collection, ImportAnkiPackageOptions, ImportAnkiPackageRequest  # noqa: E402

TODAY = [
    "Avez-vous des allergies à des médicaments ? ; Do you have any medication allergies? ; pharmacie shadow",
    "Un moment, s'il vous plaît. ; One moment, please. ; pharmacie la_vie",
    "Le vol est en retard. ; The flight is late. ; aviation",
    "Comment s'est passée ta journée ? ; How was your day? ; ecole shadow",
    "Ça marche. ; Sounds good / That works. ; la_vie",
]
PAIRS = ["sous ; sur ; under ; on ; ou vs u", "pain ; bain ; bread ; bath ; p vs b"]


class FakeTTS:
    def __init__(self):
        self.calls = []

    def __call__(self, text, slow, dest, voice=None):
        self.calls.append((text, slow))
        self.voices = getattr(self, "voices", []) + [voice]
        Path(dest).write_bytes(b"ID3fake-mp3:" + text.encode() + (b":slow" if slow else b""))


def fname(text, slow):
    """Expected media filename for a chunk's audio."""
    return fa.audio_filename(text, slow, fa.voice_for(text, text))


@pytest.fixture
def work(tmp_path):
    w = {
        "chunks": tmp_path / "chunks_master.csv",
        "pairs": tmp_path / "pairs_master.csv",
        "media": tmp_path / "media",
        "out": tmp_path / "Francais_Sherif.apkg",
        "tts": FakeTTS(),
        "tmp": tmp_path,
    }
    fa.add_chunks(TODAY, path=w["chunks"], today="2026-11-24", log=lambda *_: None)
    fa.add_pairs(PAIRS, path=w["pairs"], today="2026-11-24", log=lambda *_: None)
    return w


def do_build(w, **kw):
    cache = fa.AudioCache(w["media"], synthesize=w["tts"], fallback=None, pause=0)
    return fa.build(w["chunks"], w["pairs"], w["out"], audio=cache, log=lambda *_: None, **kw)


def import_into(col_path, apkg):
    col = Collection(str(col_path))
    col.import_anki_package(ImportAnkiPackageRequest(package_path=str(apkg), options=ImportAnkiPackageOptions()))
    return col


# ---------------------------------------------------------------- CSV / append-only
def test_add_appends_and_skips_exact_duplicates(work):
    before = work["chunks"].read_bytes()
    added, skipped = fa.add_chunks(
        ["Ça  marche. ; dup with extra space ; la_vie", "Il neigeait beaucoup. ; It was snowing a lot. ; aviation"],
        path=work["chunks"], today="2026-11-25", log=lambda *_: None)
    assert (added, skipped) == (1, 1)
    after = work["chunks"].read_bytes()
    assert after.startswith(before), "existing rows must never be rewritten"
    rows = fa.load_chunks(work["chunks"], warn=lambda *_: None)
    assert [r.french for r in rows][-1] == "Il neigeait beaucoup."
    assert rows[-1].date_added == "2026-11-25"


def test_csv_header_and_note_column(work):
    fa.add_chunks(["Prenez-le à jeun. ; Take it on an empty stomach. ; pharmacie ; à jeun = fasting"],
                  path=work["chunks"], log=lambda *_: None)
    header = work["chunks"].read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == "french,english,tags,date_added,note"
    last = fa.load_chunks(work["chunks"], warn=lambda *_: None)[-1]
    assert last.note == "à jeun = fasting" and last.tags == ["pharmacie"]


def test_bad_line_raises():
    with pytest.raises(ValueError):
        fa.parse_chunk_line("only french, no english")


def test_unknown_tag_is_reported(work):
    msgs = []
    fa.add_chunks(["Bonjour. ; Hello. ; pharamcie"], path=work["chunks"], log=msgs.append)
    assert any("pharamcie" in m for m in msgs)


def test_tag_aliases_and_subdeck_priority():
    tags, unknown = fa.normalize_tags("École Shadow")
    assert tags == ["ecole", "shadow"] and not unknown
    assert fa.subdeck_for(["la_vie", "pharmacie"]) == "Français – Sherif::Pharmacie"
    assert fa.subdeck_for(["shadow"]) == "Français – Sherif"


# ---------------------------------------------------------------- GUIDs / IDs
def test_guid_is_stable_and_whitespace_insensitive():
    a = fa.chunk_guid("Ça marche.")
    assert a == fa.chunk_guid("  Ça  marche. ")
    assert a != fa.chunk_guid("Ça marche !")
    assert a == "%s" % fa.chunk_guid("Ça marche.")  # deterministic across calls


def test_fixed_ids_never_change():
    # If this fails, someone changed an ID: future imports would create NEW decks/note types.
    assert fa.DECK_IDS["Français – Sherif"] == 1135215982792
    assert fa.MODEL_ID_CHUNK == 1585937399213 and fa.MODEL_ID_PAIRE == 1492339166531
    assert len(set(fa.DECK_IDS.values())) == 6


# ---------------------------------------------------------------- audio cache
def test_audio_cached_and_not_regenerated(work):
    do_build(work)
    first = len(work["tts"].calls)
    assert first == 2 * len(TODAY) + 2 * len(PAIRS)
    assert ("Ça marche.", True) in work["tts"].calls and ("Ça marche.", False) in work["tts"].calls
    do_build(work)
    assert len(work["tts"].calls) == first, "second build must use the cache"


def test_empty_cached_file_is_regenerated(work):
    do_build(work)
    victim = work["media"] / fname("Ça marche.", slow=False)
    victim.write_bytes(b"")
    n = len(work["tts"].calls)
    do_build(work)
    assert len(work["tts"].calls) == n + 1 and victim.stat().st_size > 0


def test_audio_failure_aborts_without_writing(work):
    def broken(text, slow, dest):
        raise ConnectionError("offline")
    cache = fa.AudioCache(work["media"], synthesize=broken, fallback=broken, pause=0, retries=1)
    with pytest.raises(SystemExit):
        fa.build(work["chunks"], work["pairs"], work["out"], audio=cache, log=lambda *_: None)
    assert not work["out"].exists()


# ---------------------------------------------------------------- real Anki import
def test_import_creates_expected_decks_and_cards(work):
    res = do_build(work)
    assert res.cards == {"Listen": 5, "Speak": 5, "Shadow": 2, "Paire": 4}
    col = import_into(work["tmp"] / "c.anki2", work["out"])
    try:
        # Modern Anki matches decks by NAME on import (it assigns local deck ids), while
        # note types keep our fixed ids.
        for name in fa.DECK_IDS:
            assert col.decks.by_name(name) is not None, name
        assert col.models.get(fa.MODEL_ID_CHUNK)["name"] == "FR Chunk"
        assert col.models.get(fa.MODEL_ID_PAIRE)["name"] == "FR Paire"
        assert col.note_count() == 7
        assert col.card_count() == 5 + 5 + 2 + 4
        shadow_notes = {col.get_card(c).note()["French"] for c in col.find_cards('card:"3 Shadow"')}
        assert shadow_notes == {"Avez-vous des allergies à des médicaments ?", "Comment s'est passée ta journée ?"}
        assert len(col.find_cards('"deck:Français – Sherif::Pharmacie"')) == 2 + 2 + 1  # 2 notes, 1 shadow
        assert len(col.find_cards('"deck:Français – Sherif::Prononciation"')) == 4
        # media files actually ended up in the collection
        media = set(Path(col.media.dir()).iterdir())
        assert len(media) == 2 * 5 + 4
    finally:
        col.close()


def test_card_faces(work):
    do_build(work)
    col = import_into(work["tmp"] / "c.anki2", work["out"])
    try:
        nid = col.find_notes('"French:Ça marche."')[0]
        cards = {c.template()["name"]: c for c in col.get_note(nid).cards()}
        assert set(cards) == {"1 Listen", "2 Speak"}  # not tagged shadow -> no shadow card
        listen_q = cards["1 Listen"].question()
        assert "Ça marche" not in listen_q and "Sounds good" not in listen_q  # no text on LISTEN front
        assert "Écoutez et répétez" in listen_q and "frsPlay('frs_" in listen_q
        slow_name = fname("Ça marche.", slow=True)
        normal_name = fname("Ça marche.", slow=False)
        q_sounds = [t.filename for t in cards["1 Listen"].question_av_tags()]
        assert q_sounds == [slow_name], "only the SLOW audio autoplays on the LISTEN front"
        assert "Sounds good" in cards["1 Listen"].answer() and "Ça marche." in cards["1 Listen"].answer()

        speak_q = cards["2 Speak"].question()
        assert "Sounds good" in speak_q and "Ça marche" not in speak_q
        assert [t.filename for t in cards["2 Speak"].question_av_tags()] == []
        assert [t.filename for t in cards["2 Speak"].answer_av_tags()] == [normal_name]

        sid = col.find_notes('"French:Avez-vous des allergies à des médicaments ?"')[0]
        sh = [c for c in col.get_note(sid).cards() if c.template()["name"] == "3 Shadow"][0]
        assert "Avez-vous" in sh.question() and "Répétez 3 fois" in sh.answer()
        assert len(sh.answer_av_tags()) == 1

        pid = col.find_notes('"Word1:sous"')[0]
        p1, p2 = sorted(col.get_note(pid).cards(), key=lambda c: c.ord)
        pv = fa.pair_voice("sous", "sur")
        assert [t.filename for t in p1.question_av_tags()] == [fa.audio_filename("sous", False, pv)]
        assert [t.filename for t in p2.question_av_tags()] == [fa.audio_filename("sur", False, pv)]
        assert "under" in p1.answer() and "on" in p1.answer() and "ou vs u" in p1.answer()
    finally:
        col.close()


def test_reimport_merges_keeps_history_and_updates(work):
    do_build(work)
    col_path = work["tmp"] / "c.anki2"
    col = import_into(col_path, work["out"])
    try:
        # Review a card so it has history.
        cid = col.find_cards('"French:Ça marche." card:"2 Speak"')[0]
        card = col.get_card(cid)
        card.start_timer()
        col.sched.answerCard(card, 3)
        card = col.get_card(cid)
        reps_before, revlog_before = card.reps, col.db.scalar("select count() from revlog where cid=?", cid)
        assert reps_before == 1 and revlog_before == 1
    finally:
        col.close()

    # Next lesson: add a chunk and rebuild.
    fa.add_chunks(["Il neigeait beaucoup. ; It was snowing a lot. ; aviation"], path=work["chunks"],
                  log=lambda *_: None)
    time.sleep(1.1)  # make the new build "newer" at 1-second resolution
    do_build(work)

    col = Collection(str(col_path))
    try:
        col.import_anki_package(ImportAnkiPackageRequest(package_path=str(work["out"]),
                                                         options=ImportAnkiPackageOptions()))
        assert col.note_count() == 8, "re-import must add the new note and duplicate nothing"
        assert len(col.find_notes('"French:Ça marche."')) == 1
        card = col.get_card(cid)
        assert card.reps == reps_before
        assert col.db.scalar("select count() from revlog where cid=?", cid) == 1
        assert len(col.decks.all_names_and_ids()) == 7  # Default + our 6, no "+1" copies
        assert len(col.models.all_names_and_ids()) >= 2
        assert len([m for m in col.models.all_names_and_ids() if m.name.startswith("FR Chunk")]) == 1
    finally:
        col.close()


def test_apkg_contains_only_referenced_media(work):
    do_build(work)
    with zipfile.ZipFile(work["out"]) as z:
        mapping = json.loads(z.read("media"))
    assert len(mapping) == 14 and all(name.startswith("frs_") for name in mapping.values())


# ---------------------------------------------------------------- AnkiConnect push
class _FakeAnkiConnect(http.server.BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append(req)
        result = {"version": 6, "importPackage": True, "sync": None}.get(req["action"])
        body = json.dumps({"result": result, "error": None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_push_uses_importpackage_with_absolute_path(work):
    do_build(work)
    _FakeAnkiConnect.calls = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeAnkiConnect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ok = fa.push_to_anki(work["out"], url=f"http://127.0.0.1:{srv.server_port}", log=lambda *_: None)
    finally:
        srv.shutdown()
    assert ok
    actions = [c["action"] for c in _FakeAnkiConnect.calls]
    assert actions == ["version", "importPackage", "sync"]
    assert Path(_FakeAnkiConnect.calls[1]["params"]["path"]).is_absolute()


def test_push_falls_back_when_anki_not_running(work):
    do_build(work)
    msgs = []
    assert fa.push_to_anki(work["out"], url="http://127.0.0.1:9", log=msgs.append) is False
    assert any("File → Import" in m for m in msgs)


def test_design_change_updates_note_type_in_place(work, monkeypatch):
    do_build(work)
    col_path = work["tmp"] / "c.anki2"
    import_into(col_path, work["out"]).close()
    new_tmpls = [dict(t) for t in fa.CHUNK_MODEL.templates]
    new_tmpls[1]["qfmt"] = new_tmpls[1]["qfmt"].replace("Dites-le", "DITES-LE")
    monkeypatch.setattr(fa.CHUNK_MODEL, "templates", new_tmpls)
    time.sleep(1.1)
    do_build(work)
    col = import_into(col_path, work["out"])
    try:
        names = [m.name for m in col.models.all_names_and_ids() if m.name.startswith("FR ")]
        assert sorted(names) == ["FR Chunk", "FR Paire"], names
        assert "DITES-LE" in col.models.get(fa.MODEL_ID_CHUNK)["tmpls"][1]["qfmt"]
        assert col.note_count() == 7
    finally:
        col.close()


# ---------------------------------------------------------------- voices
NATURAL = {v for v, _ in fa.SPEAKERS}
FRENCH_ONLY = {v for _, v in fa.SPEAKERS}


def test_voice_is_stable_per_chunk_and_both_speakers_are_used():
    texts = [l.split(";")[0].strip() for l in TODAY] + [f"Phrase numéro {i} pour tester." for i in range(20)]
    voices = [fa.voice_for(t, t) for t in texts]
    assert voices == [fa.voice_for(t, t) for t in texts], "same text must always get the same voice"
    long_voices = {fa.voice_for(t, t) for t in texts if len(t.split()) > 2}
    assert long_voices == NATURAL, "a mix of the two chosen natural voices"


def test_short_texts_use_french_only_voice_of_same_speaker():
    v_long = fa.voice_for("Ça marche bien aujourd'hui.", "Ça marche bien aujourd'hui.")
    v_short = fa.voice_for("Ça marche bien aujourd'hui.", "Ça marche.")
    assert v_long in NATURAL and v_short in FRENCH_ONLY
    assert fa.SPEAKERS.index(next(s for s in fa.SPEAKERS if v_long in s)) == \
        fa.SPEAKERS.index(next(s for s in fa.SPEAKERS if v_short in s)), "same gender/speaker"


def test_pair_words_share_one_french_only_voice(work):
    do_build(work)
    pv = fa.pair_voice("pain", "bain")
    assert pv in FRENCH_ONLY
    assert (work["media"] / fa.audio_filename("pain", False, pv)).exists()
    assert (work["media"] / fa.audio_filename("bain", False, pv)).exists()


def test_slow_audio_uses_same_voice(work):
    do_build(work)
    t = "Avez-vous des allergies à des médicaments ?"
    v = fa.voice_for(t, t)
    assert (work["media"] / fa.audio_filename(t, True, v)).exists()
    assert fa.audio_filename(t, True, v) != fa.audio_filename(t, False, v)


def test_falls_back_to_gtts_when_neural_voice_fails(work):
    def broken(text, slow, dest, voice=None):
        raise ConnectionError("service down")
    fb = FakeTTS()
    cache = fa.AudioCache(work["media"], synthesize=broken, fallback=fb, pause=0, retries=1)
    res = fa.build(work["chunks"], work["pairs"], work["out"], audio=cache, log=lambda *_: None)
    assert work["out"].exists() and not res.audio_failed
    assert len(res.audio_fallbacks) == len(fb.calls) > 0
    assert (work["media"] / fa.audio_filename("Ça marche.", False, fa.GTTS)).exists()
