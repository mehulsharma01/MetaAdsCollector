# Apify Actor image for Meta Ads Collector.
# Uses the official Apify Python base image so the platform runs THIS code
# (via main.py) instead of the default Node.js "main.js".
FROM apify/actor-python:3.12

# Install build/runtime deps first for better layer caching.
COPY requirements.txt ./
COPY pyproject.toml ./
COPY README.md ./

# Install the Apify SDK plus the collector package and its dependencies
# (curl_cffi). We install the package itself so `import meta_ads_collector`
# works at runtime.
COPY meta_ads_collector ./meta_ads_collector
RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir "apify>=2.0.0,<3.0.0" . \
    && echo "All installed, checking imports:" \
    && python -c "import apify, curl_cffi, meta_ads_collector; print('imports OK')"

# Copy the actor entry point.
COPY main.py ./

# Run the actor.
CMD ["python", "main.py"]
