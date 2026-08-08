# Apify Actor image for Meta Ads Collector.
# Uses the official Apify Python base image so the platform runs THIS code
# (via main.py) instead of the default Node.js "main.js".
#
# We install the Apify SDK (the base image does not ship it) plus this
# project's single third-party runtime dependency, curl_cffi. Everything
# else the collector needs is in the Python standard library. We do NOT
# `pip install .` -- the collector imports directly from the copied folder
# because `python main.py` puts the working directory on sys.path.
#
# NOTE: apify must be v4+. The 2.x line fails to import against current
# pydantic ("cannot specify both default and default_factory"), which is
# what broke earlier builds at the import step.
FROM apify/actor-python:3.12

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir "apify>=4,<5" "curl_cffi>=0.7.0" "Pillow>=10.0"

# Copy the collector package and the actor entry point into the workdir.
COPY meta_ads_collector ./meta_ads_collector
COPY main.py ./

# Sanity-check imports one at a time so a failure names the exact module.
RUN python -c "import apify; print('apify OK')" \
    && python -c "import curl_cffi; print('curl_cffi OK')" \
    && python -c "import meta_ads_collector; print('meta_ads_collector OK')"

# Run the actor.
CMD ["python", "main.py"]
