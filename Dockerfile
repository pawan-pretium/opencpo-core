FROM python:3.12-slim AS builder

WORKDIR /build

# Install system build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ISO 15118 EXI codec (libcbv2g). Built here so the runtime image can
# read and write Plug and Charge certificate messages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake ninja-build git \
    && git clone --depth 1 --recurse-submodules --branch v1.0 \
        https://github.com/IdahoLabResearch/EXPy.git /tmp/EXPy \
    && pip install --no-cache-dir --prefix=/install /tmp/EXPy \
    && rm -rf /tmp/EXPy /var/lib/apt/lists/*


FROM python:3.12-slim

WORKDIR /app

# Runtime system deps (libpq for asyncpg, docker for update mechanism)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    curl \
    ca-certificates \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . .

EXPOSE 8000 9100 9201

ENV HOST=0.0.0.0
ENV OCPP_API_PORT=8000
ENV OCPP_16_WS_PORT=9100
ENV OCPP_201_WS_PORT=9201

CMD ["python", "-u", "main.py"]
