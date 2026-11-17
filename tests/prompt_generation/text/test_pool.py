from src.prompt_generation.text.domains import read_kept
from src.prompt_generation.text.pool import build_pool, features
from src.prompt_generation.text.sentences import KEPT_COLUMNS, _write_csv


def test_pool_preserves_live_ids_and_text_and_drops_normalised_duplicates(tmp_path):
    live = tmp_path / "live.csv"
    kept = tmp_path / "sentences-kept.csv"
    out = tmp_path / "pool"
    live.write_text(
        "prompt_id,text,source,domain,active,source_file,times_recorded\n"
        "P1,What’s here?,live-source,tbd,t,live.txt,3\n"
        "P2,An old line.,live-source,tbd,f,live.txt,8\n"
        "P3,Another line.,live-source,tbd,t,live.txt,0\n",
        encoding="utf-8",
    )
    _write_csv(kept, [
        {"sentence_id": sid, "text": text, "source": "nation", "source_reference": "Nation Publications"}
        for sid, text in [("S1", "WHAT'S  HERE!"), ("S2", "An old line!"), ("S3", "Short.")]
    ], KEPT_COLUMNS)
    original_inputs = live.read_bytes(), kept.read_bytes()

    build_pool(live, kept, out)

    rows = {row["pool_id"]: row for row in read_kept(out / "pool.csv")}
    assert set(rows) == {"P1", "P2", "P3", "S3"}
    assert rows["P1"]["in_use"] == "True"
    assert rows["P1"]["text"] == "What’s here?"
    assert rows["P2"]["status"] == "retired"
    assert rows["P2"]["in_use"] == rows["P3"]["in_use"] == "False"
    assert rows["S3"]["candidate_id"] == "S3"
    assert rows["S3"]["source_category"] == "nation"
    assert rows["S3"]["source_ref"] == "Nation Publications"
    dropped = read_kept(out / "pool-dropped.csv")
    assert [(r["pool_id"], r["prompt_id"], r["drop_reason"]) for r in dropped] == [
        ("S1", "P1", "duplicate-of-live"), ("S2", "P2", "duplicate-of-live"),
    ]
    assert (live.read_bytes(), kept.read_bytes()) == original_inputs


def test_features_use_the_original_text_and_inclusive_length_bounds():
    assert features("abcdefghijklmn ab   abc") == {
        "word_count": 3, "char_count": 23, "avg_word_len": 6.33,
        "long_words": 1, "est_read_s": 2.5, "in_target_length": False,
    }
    for count, expected in [(0, False), (14, False), (15, True), (55, True), (56, False)]:
        assert features(" ".join(["word"] * count))["in_target_length"] is expected
