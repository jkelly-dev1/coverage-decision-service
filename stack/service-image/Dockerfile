# The decision service's runtime, pinned by tag and not by digest. The digest
# this tag resolved to for the shipped results was not recorded, and a digest
# taken later would name an image those results were not measured on. A
# rebuild can pull a newer image under the same tag; the README says so.
FROM python:3.13-slim-bookworm

# psycopg ONLY. Nothing else is installed, and the version is pinned for the
# same reason the base image is. The binary wheel is used deliberately: the
# alternative builds libpq from source at image build time, which makes a
# clone's first run depend on a toolchain and on the network staying up.
RUN pip install --no-cache-dir "psycopg[binary]==3.2.10" \
 && python -c "import psycopg; print('psycopg', psycopg.__version__)"

WORKDIR /app
