#!/usr/bin/env bash
# Build the image locally, package it for transfer, push it into a cluster.
#
#   ./build.sh build            build the image locally
#   ./build.sh save             export it to dist/sysbench-perf.tar.gz
#   ./build.sh push             push into the internal registry of the logged-in cluster
#   ./build.sh load <tar.gz>    import an exported image on another machine
#
# Nothing here touches a cluster except `push`, which only writes an image
# into the registry - no workload changes.
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-sysbench-perf}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
LOCAL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
NAMESPACE="${NAMESPACE:-openshift}"
DIST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dist"

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

cmd_push() {
  command -v oc >/dev/null || die "oc not found"
  oc whoami >/dev/null 2>&1 || die "not logged in - run 'oc login' first"

  local registry
  registry="$(oc get route default-route -n openshift-image-registry \
    -o jsonpath='{.spec.host}' 2>/dev/null || true)"
  [[ -n "${registry}" ]] || die "the internal registry has no external route.
Expose it first (cluster-admin, a real change - ask before doing this):
  oc patch configs.imageregistry.operator.openshift.io/cluster --type=merge \\
     -p '{\"spec\":{\"defaultRoute\":true}}'
Alternatively transfer with skopeo:
  skopeo copy docker-archive:dist/${IMAGE_NAME}-${IMAGE_TAG}.tar.gz docker://<registry>/<ns>/${LOCAL_IMAGE}"

  local target="${registry}/${NAMESPACE}/${LOCAL_IMAGE}"
  info "logging in to ${registry}"
  engine login -u "$(oc whoami)" -p "$(oc whoami -t)" "${registry}"
  info "pushing ${target}"
  engine tag "${LOCAL_IMAGE}" "${target}"
  engine push "${target}"
  echo
  info "deploy with:"
  echo "  oc new-app -f deploy/template.yaml \\"
  echo "     -p IMAGE=image-registry.openshift-image-registry.svc:5000/${NAMESPACE}/${LOCAL_IMAGE}"
}

case "${1:-}" in
  build) cmd_build ;;
  save)  cmd_save ;;
  load)  shift; cmd_load "$@" ;;
  push)  cmd_push ;;
  all)   cmd_build; cmd_save ;;
  *)     sed -n '2,10p' "$0"; exit 1 ;;
esac
