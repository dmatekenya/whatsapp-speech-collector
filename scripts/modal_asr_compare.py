"""Modal: transcribe the same clips with several Chichewa ASR models and compare speed and accuracy.

Each model runs in its own GPU container, in parallel. Inference follows the project's own
evaluation code:
- Whisper (src/train/train_whisper.py::run_evaluation): WhisperProcessor + WhisperForConditionalGeneration,
  prefix tokens language="shona", task="transcribe", forced_decoder_ids cleared, attention mask passed
  to model.generate(). Whisper has no Chichewa token; the models were fine-tuned under "shona".
- MMS (src/train/train_mms.py::run_evaluation): processor from the model repo, Wav2Vec2ForCTC,
  argmax over logits, processor.batch_decode().
CER/WER use a copy of src/data_utils/text_utils.py::normalize_transcript on both sides.

Whisper clips over 30 s (LONG_FORM_SECONDS) use Whisper's own long-form generation instead of
being cut at 30 s: the feature extractor is called with truncation=False, padding="longest",
return_attention_mask=True, and generate() decodes the audio window by window with the same
Shona/transcribe settings, greedy. transformers 4.46.3 requires return_timestamps=True for this
(both models' generation_config has return_timestamps=False, and generate() refuses more than
3000 feature frames without it). return_segments=True gives each segment's tokens: each is
decoded on its own (the tokenizer drops the timestamp tokens) and the segments are joined with a
space (join_segments), since decoding the whole sequence at once runs words together at segment
boundaries. No log-probability for these clips (confidence is blank). Each row says whether it
went through long-form. The ASR batch app (scripts/modal_asr_app.py) uses the same path.

Usage (from the repo root):
    uv run modal run scripts/modal_asr_compare.py --wav data/audio/P0006/P0006_R000022.wav
    uv run modal run scripts/modal_asr_compare.py --wav data/audio/P0006/P0006_R000022.wav --reference "..."
    uv run modal run scripts/modal_asr_compare.py --wav data/audio/P0006 --limit 10
    uv run modal run scripts/modal_asr_compare.py --manifest data/review/asr_sample.csv   # columns: path,reference
    uv run modal run scripts/modal_asr_compare.py --wav <...> --models whisper_b,mms
    uv run modal run scripts/modal_asr_compare.py --wav <a WAV over 30 s> --models whisper_b,whisper_v1   # long-form

Only HF_TOKEN is read from the local .env and sent to Modal.
"""

# =======
# IMPORTS
# =======
import csv
from pathlib import Path

import modal

# ======
# MODELS
# ======
# kind: "whisper" or "mms". revision: a commit hash or branch (None = main).
MODELS = {
    "whisper_b": {
        "kind": "whisper",
        "repo": "dmatekenya/whisper-large-v3-chichewa-variant-b-normalized-transcript",
        "revision": None,
    },
    "whisper_v1": {
        "kind": "whisper",
        "repo": "dmatekenya/whisper-large-v3-chichewa",
        # The model card says main is the last checkpoint (WER ~101) and the best one (WER ~61)
        # is an earlier commit. Put that commit hash here.
        "revision": None,
    },
    "mms": {
        "kind": "mms",
        "repo": "dmatekenya/mms-1b-chichewa",
        "revision": None,
        # Only needed if the adapter was saved as a separate adapter.<lang>.safetensors file.
        "target_lang": None,
    },
}

WHISPER_BASE = "openai/whisper-large-v3"   # processor source for both Whisper models, as in training
LANGUAGE = "shona"
TASK = "transcribe"
WHISPER_DTYPE = "float16"                  # "float32" matches training exactly
GPU = ["L4", "A10G", "T4"]                 # first free GPU type wins
SAMPLE_RATE = 16000
LONG_FORM_SECONDS = 30.0                   # Whisper's window: longer clips use long-form generation

# ===========
# MODAL SETUP
# ===========
app = modal.App("tiyankhuleni-asr-compare")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "torch==2.5.1",
        "transformers==4.46.3",
        "accelerate==1.1.1",
        "safetensors==0.4.5",
        "soundfile==0.12.1",
        "librosa==0.10.2.post1",
        "jiwer==3.0.5",
        "numpy<2.1",
    )
    .env({"HF_HOME": "/cache/hf"})
)

model_cache = modal.Volume.from_name("hf-model-cache", create_if_missing=True)


def _hf_token_from_dotenv() -> str:
    """Read only HF_TOKEN from the local .env (empty string if absent)."""
    if not modal.is_local():
        return ""
    from dotenv import dotenv_values

    return (dotenv_values(".env").get("HF_TOKEN") or "").strip()


hf_secret = modal.Secret.from_dict({"HF_TOKEN": _hf_token_from_dotenv()})


# =============
# NORMALISATION
# =============
# Copy of src/data_utils/text_utils.py::normalize_transcript (default settings).
_APOS = "'"
_APOSTROPHE_VARIANTS = {c: _APOS for c in "’‘‛ʼʹʻ`´�"}


def normalize_transcript(text: str) -> str:
    import re
    import unicodedata

    if not text or not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(str.maketrans(_APOSTROPHE_VARIANTS))
    text = re.sub(r"\r\n|\r|\n", " ", text)
    text = re.sub(r"\[.*?\]", " ", text)
    text = re.sub(r"\(.*?\)", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "P" or ch == _APOS)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


# ==========
# TRANSCRIBE
# ==========
def join_segments(texts: list[str]) -> str:
    """One transcript from long-form segment texts: joined with a single space, whitespace collapsed.
    The same as scripts/modal_asr_app.py::join_segments."""
    import re

    return re.sub(r"\s+", " ", " ".join(texts)).strip()


def whisper_call_kwargs(audio_seconds: float) -> tuple[dict, dict, bool]:
    """(feature extractor kwargs, generate() kwargs, long_form) for one Whisper clip.

    Up to LONG_FORM_SECONDS: as before (the feature extractor pads or cuts to 30 s, and
    generate() returns scores for the confidence). Longer: no truncation, padded to the clip
    itself, attention mask, and long-form generate(), which needs return_timestamps=True in
    transformers 4.46.3 (these models' generation_config sets it to False), returning segments.
    """
    fe = {"sampling_rate": SAMPLE_RATE, "return_attention_mask": True, "return_tensors": "pt"}
    if audio_seconds <= LONG_FORM_SECONDS:
        return fe, {"return_dict_in_generate": True, "output_scores": True}, False
    fe.update(truncation=False, padding="longest")
    return fe, {"return_timestamps": True, "return_segments": True}, True


@app.function(gpu=GPU, image=image, volumes={"/cache": model_cache}, secrets=[hf_secret], timeout=1800)
def transcribe(model_key: str, clips: list) -> dict:
    """clips: list of (name, wav_bytes, reference). Returns timings and per-clip results."""
    import io
    import os
    import time

    import jiwer
    import librosa
    import soundfile as sf
    import torch

    spec = MODELS[model_key]
    token = os.environ.get("HF_TOKEN") or None
    rev = spec.get("revision")

    # Load
    start = time.time()
    if spec["kind"] == "whisper":
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        dtype = torch.float16 if WHISPER_DTYPE == "float16" else torch.float32
        # As in training: the processor always comes from the base checkpoint (same tokenizer and
        # feature extractor). The fine-tuned repos lack the slow-tokenizer files (vocab.json).
        processor = WhisperProcessor.from_pretrained(WHISPER_BASE, language=LANGUAGE, task=TASK)
        processor_source = WHISPER_BASE
        model = WhisperForConditionalGeneration.from_pretrained(
            spec["repo"], revision=rev, torch_dtype=dtype, token=token
        )
        model.to("cuda").eval()
        processor.tokenizer.set_prefix_tokens(language=LANGUAGE, task=TASK)
        model.generation_config.language = LANGUAGE
        model.generation_config.task = TASK
        model.generation_config.forced_decoder_ids = None
        if hasattr(model.config, "forced_decoder_ids"):
            model.config.forced_decoder_ids = None
    else:
        from transformers import AutoProcessor, Wav2Vec2ForCTC

        dtype = torch.float32
        processor = AutoProcessor.from_pretrained(spec["repo"], revision=rev, token=token)
        processor_source = spec["repo"]
        kwargs = {"revision": rev, "torch_dtype": dtype, "token": token}
        if spec.get("target_lang"):
            kwargs.update(target_lang=spec["target_lang"], ignore_mismatched_sizes=True)
        model = Wav2Vec2ForCTC.from_pretrained(spec["repo"], **kwargs)
        model.to("cuda").eval()
    load_seconds = time.time() - start
    model_cache.commit()

    # Transcribe each clip
    rows = []
    for name, wav_bytes, reference in clips:
        audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)

        torch.cuda.synchronize()
        start = time.time()
        long_form = False
        if spec["kind"] == "whisper":
            fe_kwargs, gen_kwargs, long_form = whisper_call_kwargs(len(audio) / SAMPLE_RATE)
            processed = processor.feature_extractor(audio, **fe_kwargs)
            gen = {"input_features": processed.input_features.to("cuda", dtype=dtype)}
            if "attention_mask" in processed:
                gen["attention_mask"] = processed.attention_mask.to("cuda")
            with torch.no_grad():
                out = model.generate(**gen, **gen_kwargs)
            if long_form:
                # Each segment decoded on its own (timestamps dropped), then joined with a space.
                tokens = [segment["tokens"] for segment in out["segments"][0]]
                text = join_segments(processor.tokenizer.batch_decode(tokens, skip_special_tokens=True))
                confidence = None
            else:
                text = processor.tokenizer.batch_decode(out.sequences, skip_special_tokens=True)[0].strip()
                scores = model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
                confidence = float(scores[0].float().mean().item()) if scores.numel() else float("nan")
        else:
            inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
            with torch.no_grad():
                logits = model(
                    inputs.input_values.to("cuda"),
                    attention_mask=inputs.attention_mask.to("cuda") if "attention_mask" in inputs else None,
                ).logits
            ids = torch.argmax(logits, dim=-1)
            text = processor.batch_decode(ids)[0].strip()
            # mean max-softmax probability per frame
            confidence = float(torch.softmax(logits.float(), dim=-1).max(dim=-1).values.mean().item())
        torch.cuda.synchronize()
        seconds = time.time() - start

        row = {
            "clip": name,
            "audio_seconds": round(len(audio) / 16000, 2),
            "seconds": round(seconds, 3),
            "confidence": None if confidence is None else round(confidence, 3),
            "long_form": long_form,
            "text": text,
        }
        if reference:
            ref_n, hyp_n = normalize_transcript(reference), normalize_transcript(text)
            row["cer"] = round(jiwer.cer(ref_n, hyp_n), 3) if ref_n else None
            row["wer"] = round(jiwer.wer(ref_n, hyp_n), 3) if ref_n else None
        rows.append(row)

    return {
        "model": model_key,
        "repo": spec["repo"],
        "revision": rev or "main",
        "processor": processor_source,
        "gpu": torch.cuda.get_device_name(0),
        "load_seconds": round(load_seconds, 1),
        "rows": rows,
    }


# ===========
# ENTRY POINT
# ===========
def _collect_clips(wav: str, reference: str, manifest: str, limit: int) -> list:
    if manifest:
        with open(manifest, newline="", encoding="utf-8") as f:
            items = [(Path(r["path"]), r.get("reference", "") or "") for r in csv.DictReader(f)]
    else:
        p = Path(wav)
        if p.is_dir():
            items = [(w, "") for w in sorted(p.glob("*.wav"))]
        else:
            items = [(p, reference)]
    items = items[:limit] if limit else items
    missing = [str(p) for p, _ in items if not p.exists()]
    if missing:
        raise SystemExit(f"File(s) not found: {missing}")
    return [(p.name, p.read_bytes(), ref) for p, ref in items]


@app.local_entrypoint()
def main(wav: str = "", reference: str = "", manifest: str = "", models: str = "", limit: int = 0):
    if not wav and not manifest:
        raise SystemExit("Pass --wav <file or folder> or --manifest <csv with path,reference>")
    keys = [k.strip() for k in models.split(",") if k.strip()] or list(MODELS)
    unknown = [k for k in keys if k not in MODELS]
    if unknown:
        raise SystemExit(f"Unknown model(s): {unknown}. Choose from {list(MODELS)}")

    clips = _collect_clips(wav, reference, manifest, limit)
    print(f"{len(clips)} clip(s) x {len(keys)} model(s), running in parallel...\n")

    results = list(transcribe.starmap([(k, clips) for k in keys], return_exceptions=True))

    # Per-model summary
    print(f"{'model':<12}{'gpu':<26}{'load s':>8}{'audio s':>9}{'asr s':>8}{'RTF':>7}{'CER':>7}{'WER':>7}")
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            print(f"{key:<12}FAILED: {str(res).splitlines()[0][:100]}")
            continue
        rows = res["rows"]
        audio_s = sum(r["audio_seconds"] for r in rows)
        asr_s = sum(r["seconds"] for r in rows)
        scored = [r for r in rows if r.get("cer") is not None]
        cer = f"{sum(r['cer'] for r in scored) / len(scored):.3f}" if scored else "-"
        wer = f"{sum(r['wer'] for r in scored) / len(scored):.3f}" if scored else "-"
        rtf = f"{asr_s / audio_s:.3f}" if audio_s else "-"
        print(f"{key:<12}{res['gpu'][:25]:<26}{res['load_seconds']:>8}{audio_s:>9.1f}{asr_s:>8.2f}{rtf:>7}{cer:>7}{wer:>7}")

    # Per-clip transcripts side by side
    ok = {k: r for k, r in zip(keys, results) if not isinstance(r, Exception)}
    for i, (name, _, ref) in enumerate(clips):
        print(f"\n--- {name}")
        if ref:
            print(f"  {'reference':<12}{ref}")
        for key, res in ok.items():
            r = res["rows"][i]
            extra = f"  [CER {r['cer']}]" if r.get("cer") is not None else ""
            if r.get("long_form"):
                extra += f"  [long-form, {r['audio_seconds']} s]"
            print(f"  {key:<12}{r['text']}{extra}")

    for key, res in ok.items():
        print(f"\n{key}: {res['repo']}@{res['revision']}, processor from {res['processor']}")
