#!/usr/bin/env bash
# WSL에서 이미지를 빌드해 운영 서버로 직접 배포한다.
# GitHub Actions를 쓸 수 없는 동안 사용하는 수동 배포 경로다.
#
# 사용법:
#   bash scripts/deploy-from-wsl.sh                  # 빌드 → 전송 → 재기동 → 헬스체크
#   DEPLOY_DOCS=1 bash scripts/deploy-from-wsl.sh    # docs/*.md 를 고쳤을 때 RAG 색인까지
#
# 환경 변수로 접속 정보를 바꿀 수 있다 (SSH_KEY / SSH_TARGET / DEPLOY_PATH / IMAGE_TAG).
set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/investment-analysis-key.pem}"
# 인스턴스를 중지했다 켜면 퍼블릭 IP 가 바뀐다. 탄력적 IP 를 붙이기 전까지는
# IP 가 바뀔 때마다 아래 기본값을 고치거나 SSH_TARGET 환경변수로 넘겨서 쓴다.
SSH_TARGET="${SSH_TARGET:-ubuntu@13.209.72.115}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/investment-analysis}"
IMAGE_TAG="${IMAGE_TAG:-investment-analysis-backend:local}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh_run() { ssh -i "$SSH_KEY" "$SSH_TARGET" "$@"; }

echo "[1/5] 이미지 빌드: $IMAGE_TAG"
docker build -t "$IMAGE_TAG" "$REPO_ROOT"

echo "[2/5] 이미지 전송 (압축해서 SSH 로 직접 load — 레지스트리 불필요)"
docker save "$IMAGE_TAG" | gzip -1 | ssh_run 'gunzip | sudo docker load'

echo "[3/5] compose 파일과 API 키 동기화"
scp -q -i "$SSH_KEY" "$REPO_ROOT/docker-compose.prod.yml" "$SSH_TARGET:$DEPLOY_PATH/docker-compose.prod.yml"
if [[ -f "$REPO_ROOT/app/backend/.env" ]]; then
  scp -q -i "$SSH_KEY" "$REPO_ROOT/app/backend/.env" "$SSH_TARGET:$DEPLOY_PATH/app/backend/.env"
  ssh_run "chmod 600 $DEPLOY_PATH/app/backend/.env"
else
  echo "  (app/backend/.env 없음 — 서버에 이미 있는 파일을 그대로 사용한다)"
fi
# 이 서버의 sudo 는 -E 를 줘도 환경변수를 자식 프로세스로 넘기지 않는다(Ubuntu 26.04 sudo-rs).
# 그래서 이미지 지정은 반드시 compose 프로젝트 디렉터리의 .env 파일로 한다.
ssh_run "printf 'BACKEND_IMAGE=%s\n' '$IMAGE_TAG' > $DEPLOY_PATH/.env"

echo "[4/5] 컨테이너 재기동"
ssh_run "cd $DEPLOY_PATH && sudo docker compose -f docker-compose.prod.yml up -d && sudo docker image prune -f"

if [[ "${DEPLOY_DOCS:-0}" == "1" ]]; then
  echo "[+] RAG 문서 색인"
  ssh_run "cd $DEPLOY_PATH && sudo docker compose -f docker-compose.prod.yml exec -T backend bash /app/scripts/upload_docs_to_qdrant.sh"
fi

echo "[5/5] 헬스체크"
ssh_run 'for i in $(seq 1 30); do curl -sf http://localhost:8801/api/health && exit 0; sleep 5; done; echo "헬스체크 실패"; exit 1'
echo
echo "배포 완료: http://${SSH_TARGET#*@}:8801"
