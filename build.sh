#!/usr/bin/env bash
# Build the image locally, package it for transfer, push it into a cluster.
#
#   ./build.sh build            build the image locally
#   ./build.sh save             export it to dist/sysbench-perf.tar.gz
#   ./build.sh package          export + split into image/ chunks, committable to git
#   ./build.sh unpack           reassemble image/ chunks and import the image
#   ./build.sh push            push into the cluster registry (route, tunnel, or $REGISTRY)
#   ./build.sh push-local       force the tunnel path even if a route exists
#   ./build.sh load <tar.gz>    import an exported image on another machine
#
# Nothing here touches a cluster except `push`, which only writes an image
# into the registry - no workload changes.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-sysbench-perf}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
LOCAL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
NAMESPACE="${NAMESPACE:-openshift}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="${REPO_DIR}/dist"
# Chunks live in the repo so `git clone` alone delivers a runnable image.
IMAGE_DIR="${REPO_DIR}/image"
# GitHub hard-rejects any single file over 100MB; stay well under it.
CHUNK_SIZE="${CHUNK_SIZE:-90M}"

# podman if present, docker otherwise
ENGINE="${ENGINE:-$(command -v podman >/dev/null 2>&1 && echo podman || echo docker)}"
# Set SUDO=sudo when the container engine needs root. Only the engine is
# elevated - oc keeps running as the invoking user, with their cluster login.
SUDO="${SUDO:-}"

engine() { ${SUDO} "${ENGINE}" "$@"; }

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

cmd_build() {
  info "building ${LOCAL_IMAGE} with ${ENGINE}"
  engine build -f Containerfile -t "${LOCAL_IMAGE}" .
  info "done: $(engine images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep "^${LOCAL_IMAGE} " || echo "${LOCAL_IMAGE}")"
}

cmd_save() {
  mkdir -p "${DIST_DIR}"
  local out="${DIST_DIR}/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"
  info "exporting ${LOCAL_IMAGE} -> ${out}"
  engine save "${LOCAL_IMAGE}" | gzip -9 > "${out}"
  ( cd "${DIST_DIR}" && sha256sum "$(basename "${out}")" > "$(basename "${out}").sha256" )
  info "$(du -h "${out}" | cut -f1) - copy this file to the target environment"
  info "checksum: $(cat "${out}.sha256")"
}

cmd_load() {
  local archive="${1:-}"
  [[ -f "${archive}" ]] || die "usage: $0 load <path-to-tar.gz>"
  info "importing ${archive}"
  gunzip -c "${archive}" | engine load
}

cmd_package() {
  cmd_save
  local src="${DIST_DIR}/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"
  rm -rf "${IMAGE_DIR}"
  mkdir -p "${IMAGE_DIR}"
  info "splitting into ${CHUNK_SIZE} chunks -> image/"
  split -b "${CHUNK_SIZE}" -d -a 2 "${src}" "${IMAGE_DIR}/${IMAGE_NAME}.tar.gz.part"
  ( cd "${IMAGE_DIR}" && sha256sum ./*.part?? > SHA256SUMS )
  cp "${src}.sha256" "${IMAGE_DIR}/image.tar.gz.sha256"
  info "$(ls -1 "${IMAGE_DIR}"/*.part?? | wc -l) chunk(s):"
  ls -lh "${IMAGE_DIR}" | tail -n +2 | awk '{print "    " $9 "  " $5}'
  info "commit the image/ directory; offline side runs: ./build.sh unpack"
}

cmd_unpack() {
  [[ -d "${IMAGE_DIR}" ]] || die "no image/ directory - was the repo packaged with './build.sh package'?"
  local parts=( "${IMAGE_DIR}"/*.part?? )
  [[ -e "${parts[0]}" ]] || die "no chunks found in ${IMAGE_DIR}"

  info "verifying ${#parts[@]} chunk(s)"
  ( cd "${IMAGE_DIR}" && sha256sum -c SHA256SUMS ) || die "chunk checksum mismatch - transfer was incomplete"

  mkdir -p "${DIST_DIR}"
  local out="${DIST_DIR}/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz"
  info "reassembling -> ${out}"
  cat "${parts[@]}" > "${out}"
  # The offline side never ran `save`, so take the archive checksum from the repo.
  cp "${IMAGE_DIR}/image.tar.gz.sha256" "${out}.sha256"
  ( cd "${DIST_DIR}" && sha256sum -c "$(basename "${out}").sha256" ) \
    || die "reassembled archive does not match its checksum"

  cmd_load "${out}"
  info "image imported - continue with: ./build.sh push"
}

# podman verifies TLS on localhost; docker already treats 127.0.0.0/8 as insecure
tls_flags() {
  if [[ "${1:-}" == "insecure" && "${ENGINE}" == *podman* ]]; then
    echo "--tls-verify=false"
  fi
  # Must succeed even when it prints nothing: the caller assigns it under set -e.
  return 0
}

do_push() {
  local registry="$1" mode="${2:-}"
  local target="${registry}/${NAMESPACE}/${LOCAL_IMAGE}"
  local flags
  flags="$(tls_flags "${mode}")"

  info "logging in to ${registry}"
  engine login ${flags} -u "$(oc whoami)" -p "$(oc whoami -t)" "${registry}"
  info "pushing ${target}"
  engine tag "${LOCAL_IMAGE}" "${target}"
  engine push ${flags} "${target}"
  echo
  info "deploy with:"
  echo "  oc new-app -f deploy/template.yaml \\"
  echo "     -p IMAGE=image-registry.openshift-image-registry.svc:5000/${NAMESPACE}/${LOCAL_IMAGE}"
}

# Push through a local tunnel to the registry Service. Needs no route and no
# cluster change at all - just port-forward rights in openshift-image-registry.
push_via_port_forward() {
  local port="${LOCAL_PORT:-5000}"
  while (echo >"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; do
    info "port ${port} is busy, trying $((port + 1))"
    port=$((port + 1))
  done

  info "no registry route - tunnelling to svc/image-registry on 127.0.0.1:${port}"
  local pf_log
  pf_log="$(mktemp)"
  oc port-forward -n openshift-image-registry svc/image-registry "${port}:5000" >"${pf_log}" 2>&1 &
  local pf_pid=$!
  # shellcheck disable=SC2064
  trap "kill ${pf_pid} 2>/dev/null || true; rm -f '${pf_log}'" EXIT

  local ready=""
  for _ in $(seq 1 60); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then ready=yes; break; fi
    # No point waiting out the timeout if oc already gave up.
    kill -0 "${pf_pid}" 2>/dev/null || break
    sleep 0.5
  done

  if [[ -z "${ready}" ]]; then
    echo "--- oc port-forward output ---" >&2
    cat "${pf_log}" >&2
    echo "------------------------------" >&2
    die "the tunnel did not come up. Diagnose with:
  oc auth can-i create pods/portforward -n openshift-image-registry
  oc get configs.imageregistry.operator.openshift.io/cluster -o jsonpath='{.spec.managementState}'
  oc get pods -n openshift-image-registry
  oc get endpoints image-registry -n openshift-image-registry

If the registry is Removed or you have no access to that namespace, push to
your corporate registry instead:
  REGISTRY=rejestr.firma.local ${SUDO:+SUDO=${SUDO} }NAMESPACE=${NAMESPACE} $0 push"
  fi

  do_push "127.0.0.1:${port}" insecure
  kill "${pf_pid}" 2>/dev/null || true
  trap - EXIT
}

cmd_push() {
  command -v oc >/dev/null || die "oc not found"
  oc whoami >/dev/null 2>&1 || die "not logged in - run 'oc login' first"

  # 1. an explicitly given registry (corporate Quay/Harbor mirror) wins
  if [[ -n "${REGISTRY:-}" ]]; then
    info "using REGISTRY=${REGISTRY}"
    do_push "${REGISTRY}"
    return
  fi

  # 2. the internal registry's own route, if someone exposed it
  local registry
  registry="$(oc get route default-route -n openshift-image-registry \
    -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  if [[ -n "${registry}" ]]; then
    do_push "${registry}"
    return
  fi

  # 3. no route: tunnel to the Service instead of asking for a cluster change
  push_via_port_forward
}

case "${1:-}" in
  build) cmd_build ;;
  save)  cmd_save ;;
  package) cmd_package ;;
  unpack)  cmd_unpack ;;
  load)  shift; cmd_load "$@" ;;
  push)  cmd_push ;;
  push-local) push_via_port_forward ;;
  all)   cmd_build; cmd_package ;;
  *)     sed -n '2,12p' "$0"; exit 1 ;;
esac
