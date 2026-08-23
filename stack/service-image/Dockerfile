# The decision service's runtime. Pinned, because a latency measurement taken
# against a floating base image is a measurement of whatever was on the
# registry that day.
FROM python:3.13-slim-bookworm

# psycopg ONLY. Nothing else is installed, and the version is pinned for the
# same reason the base image is. The binary wheel is used deliberately: the
# alternative builds libpq from source at image build time, which makes a
# clone's first run depend on a toolchain and on the network staying up.
RUN pip install --no-cache-dir "psycopg[binary]==3.2.10" \
 && python -c "import psycopg; print('psycopg', psycopg.__version__)"

WORKDIR /app
