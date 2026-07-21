#!/bin/bash
# EXP-9 data seeding: diversified synthetic HR feedback over real SUCCEEDED
# runs (feedback content varies; the reward pipeline downstream is fully real),
# then rerun the reward sensitivity analysis.
set -u
cd /opt/resumai-src
set -a; source .env; set +a

RUN_IDS=$(docker exec resumai-mysql mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" \
  -e "SELECT run_id FROM agent_run WHERE status='SUCCEEDED' ORDER BY created_at DESC LIMIT 24")
echo "runs to feedback:"; echo "$RUN_IDS" | wc -l

i=0
for RID in $RUN_IDS; do
  i=$((i+1))
  # deterministic diversity: cycle through accept/reject and rating levels
  case $((i % 6)) in
    0) BODY='{"accepted":true,"ratingScore":5,"recommendationAgreed":true,"scoreDelta":0,"missedEvidenceCount":0,"unsupportedClaimCount":0,"riskJudgementCorrect":true,"comment":"exp9 强同意"}';;
    1) BODY='{"accepted":true,"ratingScore":4,"recommendationAgreed":true,"scoreDelta":0.5,"missedEvidenceCount":1,"unsupportedClaimCount":0,"riskJudgementCorrect":true,"comment":"exp9 轻微遗漏"}';;
    2) BODY='{"accepted":true,"ratingScore":3,"recommendationAgreed":false,"scoreDelta":1,"missedEvidenceCount":1,"unsupportedClaimCount":1,"riskJudgementCorrect":true,"comment":"exp9 推荐分歧"}';;
    3) BODY='{"accepted":false,"ratingScore":2,"recommendationAgreed":false,"scoreDelta":-1.5,"missedEvidenceCount":2,"unsupportedClaimCount":1,"riskJudgementCorrect":false,"comment":"exp9 风险误判"}';;
    4) BODY='{"accepted":true,"ratingScore":4,"recommendationAgreed":true,"scoreDelta":-0.5,"missedEvidenceCount":0,"unsupportedClaimCount":1,"riskJudgementCorrect":true,"comment":"exp9 有未支撑结论"}';;
    5) BODY='{"accepted":false,"ratingScore":1,"recommendationAgreed":false,"scoreDelta":2,"missedEvidenceCount":3,"unsupportedClaimCount":2,"riskJudgementCorrect":false,"comment":"exp9 强不同意"}';;
  esac
  OUT=$(curl -sS -X POST "http://127.0.0.1/api/runs/$RID/feedback" -H 'Content-Type: application/json' -d "$BODY" || echo fail)
  echo "$i $RID -> $(echo "$OUT" | head -c 120)"
done

echo "=== rerun reward sensitivity ==="
python3 harness/run_reward_sensitivity.py || true
cat reports/experiments/reward_sensitivity.json
echo EXP9_DONE
