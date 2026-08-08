# Apify Actor image for Trend Radar (Google Trends via pytrends).
FROM apify/actor-python:3.12

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir "apify>=4,<5" "pytrends>=4.9.2" "pandas>=2.0" "urllib3<2"

COPY main.py ./

RUN python -c "import apify; print('apify OK')" \
    && python -c "import pytrends, pandas; print('pytrends OK')"

CMD ["python", "main.py"]
