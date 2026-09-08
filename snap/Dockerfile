FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV SNAP_INSTALLER=esa-snap_all_linux-12.0.0.sh
ENV SNAP_URL=https://download.esa.int/step/snap/12.0/installers/${SNAP_INSTALLER}
ENV SNAP_DIR=/opt/snap

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip git \
    python3.10 python3.10-venv python3-pip python3.10-dev \
    default-jdk \
    libgdal-dev gdal-bin \
    libgeos-dev libproj-dev \
    libffi-dev libssl-dev \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# Set Java home
ENV JDK_HOME="/usr/lib/jvm/default-java"
ENV JAVA_HOME=$JDK_HOME

# Install ESA SNAP 12
RUN wget -q "${SNAP_URL}" -O ${SNAP_INSTALLER} \
    && chmod +x ${SNAP_INSTALLER} \
    && ./${SNAP_INSTALLER} -q -dir ${SNAP_DIR} \
    && rm ${SNAP_INSTALLER}

# Configure esa_snappy (SNAP 12 Python bridge)
# 1. Install esa_snappy from PyPI first
# 2. Then configure it with snappy-conf
RUN pip3 install --no-cache-dir esa_snappy \
    && ${SNAP_DIR}/bin/snappy-conf /usr/bin/python3.10

# Update SNAP modules
RUN ${SNAP_DIR}/bin/snap --nosplash --nogui --modules --update-all 2>&1 || true

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /work
COPY . /work/

CMD ["python3.10"]
