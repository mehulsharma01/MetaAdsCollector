# Apify Actor image for Reddit Intel Collector.
# Uses the official Apify Python base image and runs main.py directly.
#
# The base image does not ship the Apify SDK, so we install it plus
# curl_cffi (used to fetch Reddit's JSON API with a browser-like TLS
# fingerprint). Everything else is in the Python standard library.
#
# apify must be v4+. The 2.x line fails to import against current pydantic.
FROM apify/actor-python:3.12

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir "apify>=4,<5" "curl_cffi>=0.7.0"

COPY main.py ./

RUN python -c "import apify; print('apify OK')" \
    && python -c "import curl_cffi; print('curl_cffi OK')"

CMD ["python", "main.py"]
