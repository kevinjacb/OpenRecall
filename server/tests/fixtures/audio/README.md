# Speaker-embedder integration test fixtures

Three 16 kHz mono int16 WAV clips used by
`tests/ingest/test_speaker_embedder_real.py` to verify the real
`ResemblyzerSpeakerEmbedder` separates speakers.

- `speaker_a_1.wav`, `speaker_a_2.wav` — same speaker, two utterances.
- `speaker_b_1.wav` — a different speaker.

**Source:** LibriSpeech dev-clean (https://www.openslr.org/resources/12/),
licensed CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). See the
LibriSpeech terms for attribution requirements. Clips were trimmed to ~4 s
with `tools/make_speaker_fixture.py`. LibriSpeech dev-clean ships `.flac`;
clips were decoded to 16 kHz mono wav before trimming.

If you substituted other permissive clips, update this file with their source
and license.