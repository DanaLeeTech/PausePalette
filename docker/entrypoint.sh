#!/usr/bin/env sh
# One image, two entrypoints:
#   pausepalette-entrypoint web [uvicorn args...]   -> the web UI
#   pausepalette-entrypoint plan|apply|convert|...  -> the CLI
set -e

case "${1:-web}" in
  web|serve)
    shift || true
    exec uvicorn webapp.app:app --host 0.0.0.0 --port "${PORT:-8000}" "$@"
    ;;
  cli)
    shift
    exec pausepalette "$@"
    ;;
  *)
    exec pausepalette "$@"
    ;;
esac
