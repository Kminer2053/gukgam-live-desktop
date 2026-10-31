#!/usr/bin/env bash
# 클라우드 전용 소스만 모으며 설치·접속·배포는 수행하지 않는다.
set -eu

task_root="$(cd "$(dirname "$0")/.." && pwd)"
if [ -x "$task_root/.venv/bin/python" ]; then
  task_python="$task_root/.venv/bin/python"
else
  task_python="python3"
fi
exec "$task_python" "$task_root/scripts/build_cloud.py" "$@"
