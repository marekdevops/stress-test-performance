# sysbench CPU/RAM benchmark runner for OpenShift.
#
# sysbench is not in the RHEL/UBI repositories - it lives in EPEL. This image
# pulls it from EPEL 9 at build time, so the build host needs egress. The image
# itself needs none: everything is baked in and the server uses only the Python
# standard library.
FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

LABEL name="sysbench-perf" \
      summary="sysbench CPU/memory benchmark with an HTTP results endpoint" \
      io.k8s.description="Runs sysbench cpu and sysbench memory on demand and serves the results over HTTP." \
      maintainer="platform-performance"

RUN microdnf -y --nodocs --setopt=install_weak_deps=0 install \
        python3 util-linux-core procps-ng \
 && rpm -ivh https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm \
 && microdnf -y --nodocs --setopt=install_weak_deps=0 --enablerepo=epel install sysbench \
 && microdnf clean all \
 && rm -rf /var/cache/dnf /var/cache/yum

COPY app/ /app/

# OpenShift assigns an arbitrary, unpredictable UID with GID 0. Everything the
# process must write to therefore has to be group-0 writable.
RUN mkdir -p /var/results \
 && chgrp -R 0 /app /var/results \
 && chmod -R g=u /app /var/results

ENV HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    RESULTS_DIR=/var/results

WORKDIR /app
USER 1001
EXPOSE 8080

ENTRYPOINT ["python3", "-u", "/app/server.py"]
