"""Modal app: Chichewa Whisper transcription for the ASR batch (scripts/asr_batch.py).

Deploy (from the laptop, repo root; again after changing anything in this file):
    uv run modal deploy scripts/modal_asr_app.py

The droplet never runs this file: scripts/asr_batch.py looks the deployed class up by name
(modal.Cls.from_name("tiyankhuleni-asr", "Transcriber")) and calls
Transcriber(model_key=...).transcribe(clips) with WAV bytes as arguments. Nothing is stored on
Modal except the model weights (in the hf-model-cache volume).

Inference matches scripts/modal_asr_compare.py and the training code's evaluation:
WhisperProcessor from openai/whisper-large-v3 (the fine-tuned repos have no slow-tokenizer files),
WhisperForConditionalGeneration from the model repo, prefix tokens language="shona",
task="transcribe" (Whisper has no Chichewa token; the models were fine-tuned under "shona"),
forced_decoder_ids cleared, attention mask passed to generate(), fp16, greedy decoding. The
average log-probability comes from compute_transition_scores(..., normalize_logits=True).

Runs in Python 3.11 on Modal: keep the code here 3.11-compatible.
"""

# =======
# IMPORTS
# =======
import modal

# ======
# MODELS
# ======
# Keep the keys in step with src/asr/config.py (MODEL_KEYS); a test checks they match.
# revision: a commit hash or branch (None = main).
MODELS = {
    "whisper_b": {
        "repo": "dmatekenya/whisper-large-v3-chichewa-variant-b-normalized-transcript",
        "revision": None,
    },
    "whisper_v1": {
        "repo": "dmatekenya/whisper-large-v3-chichewa",
        # The model card says main is the last checkpoint and the best one is an earlier commit:
        # put that commit hash here to use it.
        "revision": None,
    },
}
DEFAULT_MODEL = "whisper_b"
WHISPER_BASE = "openai/whisper-large-v3"  # processor source for both models, as in training
LANGUAGE = "shona"
TASK = "transcribe"
SAMPLE_RATE = 16000

# =========
# RESOURCES
# =========
APP_NAME = "tiyankhuleni-asr"
GPU = ["L4", "A10G", "T4"]  # first free GPU type wins
MAX_CONTAINERS = 2
TIMEOUT_S = 1800  # per call (one chunk of clips)
SCALEDOWN_WINDOW_S = 60  # idle containers stop after this, so the app costs nothing when idle
BATCH_SIZE = 8  # clips decoded together on the GPU

# ===========
# MODAL SETUP
# ===========
app = modal.App(APP_NAME)

# The same pins as scripts/modal_asr_compare.py.
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


# ===========
# TRANSCRIBER
# ===========
@app.cls(
    gpu=GPU,
    image=image,
    volumes={"/cache": model_cache},
    timeout=TIMEOUT_S,
    max_containers=MAX_CONTAINERS,
    scaledown_window=SCALEDOWN_WINDOW_S,
)
class Transcriber:
    model_key: str = modal.parameter(default=DEFAULT_MODEL)

    @modal.enter()
    def load(self) -> None:
        import torch
        from huggingface_hub import HfApi
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        if self.model_key not in MODELS:
            raise ValueError(f"unknown model {self.model_key!r}; available: {sorted(MODELS)}")
        spec = MODELS[self.model_key]
        self.repo = spec["repo"]
        self.dtype = torch.float16
        # Public models: no token needed. The resolved commit is stored with each transcript.
        self.revision = HfApi().model_info(self.repo, revision=spec["revision"]).sha

        self.processor = WhisperProcessor.from_pretrained(WHISPER_BASE, language=LANGUAGE, task=TASK)
        self.model = WhisperForConditionalGeneration.from_pretrained(
            self.repo, revision=self.revision, torch_dtype=self.dtype
        )
        self.model.to("cuda").eval()
        self.processor.tokenizer.set_prefix_tokens(language=LANGUAGE, task=TASK)
        self.model.generation_config.language = LANGUAGE
        self.model.generation_config.task = TASK
        self.model.generation_config.forced_decoder_ids = None
        if hasattr(self.model.config, "forced_decoder_ids"):
            self.model.config.forced_decoder_ids = None
        self.gpu = torch.cuda.get_device_name(0)
        model_cache.commit()  # keep downloaded weights for the next container

    def _audio(self, wav_bytes: bytes):
        import io

        import librosa
        import soundfile as sf

        audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
        return audio

    def _decode(self, audios: list) -> list[tuple[str, float]]:
        """(text, average log-probability) per clip, decoded together."""
        import torch

        processed = self.processor.feature_extractor(
            audios, sampling_rate=SAMPLE_RATE, return_attention_mask=True, return_tensors="pt"
        )
        gen = {"input_features": processed.input_features.to("cuda", dtype=self.dtype)}
        if "attention_mask" in processed:
            gen["attention_mask"] = processed.attention_mask.to("cuda")
        with torch.no_grad():
            out = self.model.generate(**gen, return_dict_in_generate=True, output_scores=True)
        texts = self.processor.tokenizer.batch_decode(out.sequences, skip_special_tokens=True)
        scores = self.model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
        # In a batch, finished sequences are padded after their end token: average each clip's
        # scores up to and including its first end token, as for a clip decoded alone.
        generated = out.sequences[:, -scores.shape[1] :]
        eos = self.model.generation_config.eos_token_id
        eos_ids = set(eos if isinstance(eos, list) else [eos])
        results = []
        for row, text in enumerate(texts):
            tokens = generated[row].tolist()
            end = next((i for i, token in enumerate(tokens) if token in eos_ids), len(tokens) - 1)
            row_scores = scores[row, : end + 1].float()
            logprob = float(row_scores.mean().item()) if row_scores.numel() else float("nan")
            results.append((text.strip(), logprob))
        return results

    @modal.method()
    def transcribe(self, clips: list[tuple[int, bytes]]) -> dict:
        """clips: (recording_id, WAV bytes). Per clip: recording_id, text (raw, stripped),
        avg_logprob, audio_seconds, error (None if fine). Never raises for a bad clip."""
        rows: dict[int, dict] = {}
        ready: list[tuple[int, object]] = []
        for recording_id, wav_bytes in clips:
            row = {"recording_id": recording_id, "text": None, "avg_logprob": None, "audio_seconds": None, "error": None}
            rows[recording_id] = row
            try:
                audio = self._audio(wav_bytes)
                row["audio_seconds"] = round(len(audio) / SAMPLE_RATE, 2)
                ready.append((recording_id, audio))
            except Exception as exc:  # noqa: BLE001 - reported per clip
                row["error"] = f"audio: {type(exc).__name__}: {exc}"[:500]

        for start in range(0, len(ready), BATCH_SIZE):
            batch = ready[start : start + BATCH_SIZE]
            try:
                decoded = self._decode([audio for _, audio in batch])
            except Exception:  # noqa: BLE001 - fall back to one clip at a time
                decoded = []
                for recording_id, audio in batch:
                    try:
                        decoded.append(self._decode([audio])[0])
                    except Exception as exc:  # noqa: BLE001 - reported per clip
                        rows[recording_id]["error"] = f"asr: {type(exc).__name__}: {exc}"[:500]
                        decoded.append(None)
            for (recording_id, _), result in zip(batch, decoded):
                if result is not None:
                    rows[recording_id]["text"], rows[recording_id]["avg_logprob"] = result

        return {
            "model": {"key": self.model_key, "repo": self.repo, "revision": self.revision},
            "gpu": self.gpu,
            "clips": [rows[recording_id] for recording_id, _ in clips],
        }
