FROM odoo:18.0

# Ensure we have the privileges required to install OS packages
USER root

# Install build tools required to compile Python packages such as ed25519-blake2b.
# Add the PostgreSQL repository so libpq-dev matches the libpq runtime shipped with the base image.
RUN set -eux; \
    install -d -m 0755 /usr/share/keyrings; \
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor --batch --yes -o /usr/share/keyrings/postgresql.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt noble-pgdg main" > /etc/apt/sources.list.d/postgresql-pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential python3-dev libffi-dev \
        libpq-dev libldap2-dev libsasl2-dev; \
    rm -rf /var/lib/apt/lists/*

# Install extra Python dependencies for the bundled addons
COPY Standard-odoo-addons/requirements.txt /requirements.txt
RUN pip install --no-cache-dir --break-system-packages --ignore-installed -r /requirements.txt

# Copy custom and standard addons into the image
COPY --chown=odoo:odoo Custom-odoo-addons /mnt/custom-addons
COPY --chown=odoo:odoo Standard-odoo-addons/addons /mnt/standard-addons/addons

# Ensure Odoo user owns the data directories
RUN set -eux; \
    mkdir -p /var/lib/odoo; \
    if ! getent group odoo >/dev/null; then \
        groupadd --system odoo; \
    fi; \
    if ! id -u odoo >/dev/null 2>&1; then \
        useradd --system --gid odoo --home-dir /var/lib/odoo --shell /usr/sbin/nologin odoo; \
    fi; \
    chown -R odoo:odoo /var/lib/odoo /mnt/custom-addons /mnt/standard-addons

USER odoo
WORKDIR /var/lib/odoo

EXPOSE 8069

# Keep the default entrypoint; command is provided by docker-compose
