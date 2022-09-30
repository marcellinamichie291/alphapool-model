FROM alphapool/alphapool-model:image-v0.0.1

RUN pip install --no-cache-dir tardis-dev pandas_ta fastparquet

ADD . /app
ENV ALPHAPOOL_MODEL_ID example-model-rank
ENV ALPHAPOOL_MODEL_PATH /app/data/example_model_rank.xz
ENV ALPHAPOOL_LOG_LEVEL debug
WORKDIR /app
CMD python -m src.main
