# Apify Actor image for Meta Ads Collector.
# Uses the official Apify Python base image so the platform runs THIS code
# (via main.py) instead of the default Node.js "main.js".
FROM apify/actor-python:3.12

# Install runtime dependencies only. We deliberately do NOT `pip install .`
# (the package build) -- it's unnecessary here and the project's
# pyproject metadata can trip older setuptools during the build backend
# step. Instead we install the deps directly and import the package from
# the working directory (which Python adds to sys.path when running
# `python main.py`).
RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir "apify>=1.7,<3" "curl_cffi>=0.7.0"

# Copy the collector package and the actor entry point into the workdir.
COPY meta_ads_collector ./meta_ads_collector
COPY main.py ./

# Sanity-check that everything imports before the image is finalized.
RUN echo "Checking imports:" \
    && python -c "import apify, curl_cffi, meta_ads_collector; print('imports OK')"

# Run the actor.
CMD ["python", "main.py"]
