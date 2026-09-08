FROM python:3.11-slim

# Speaker identification (matching voices to enrolled people) needs torch and
# speechbrain - about 2GB. Transcription, diarization and minutes do not, so it
# is off by default. Enable with:
#   docker compose build --build-arg WITH_SPEAKER_ID=true api
ARG WITH_SPEAKER_ID=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 curl espeak-ng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-speaker.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# CPU-only torch keeps this ~2GB rather than ~7GB when it is installed at all.
RUN if [ "$WITH_SPEAKER_ID" = "true" ]; then \
      pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 \
        --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir -r requirements-speaker.txt ; \
    else \
      echo "Building without speaker identification (~2GB smaller)" ; \
    fi

# Cache SpeechBrain model downloads on a named volume
ENV HF_HOME=/models/hf
ENV SPEECHBRAIN_CACHE=/models/speechbrain

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
